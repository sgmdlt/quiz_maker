#!/usr/bin/env python3
"""
server.py — веб-конструктор квизов. Работает только на вашем компьютере (127.0.0.1).

Запуск:
    uv run make_quiz.py serve            # откроет браузер на http://127.0.0.1:8765
    uv run make_quiz.py serve --port 9000 --no-browser
"""

import json
import mimetypes
import os
import re
import secrets
import shlex
import shutil
import threading
import time
import traceback
import urllib.parse
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import quiz_core as q
import scenario as sc
import youtube
from spectrum_ring import config_schema

WEB_DIR = Path(__file__).resolve().parent / "web"
MAX_UPLOAD = 1 << 30  # 1 ГБ

for ext, mime in {".webm": "audio/webm", ".opus": "audio/ogg", ".m4a": "audio/mp4", ".flac": "audio/flac",
                  ".ttf": "font/ttf", ".otf": "font/otf", ".mka": "audio/x-matroska", ".mp3": "audio/mpeg",
                  ".webp": "image/webp"}.items():
    mimetypes.add_type(mime, ext)


# ───────────────────────── фоновые задачи ─────────────────────────

class Job:
    def __init__(self, kind, slug):
        self.id = secrets.token_hex(6)
        self.kind, self.slug = kind, slug
        self.state = "running"
        self.progress, self.stage = 0.0, ""
        self.log = deque(maxlen=500)
        self.result = self.error = None
        self.cancel = threading.Event()
        self.started, self.finished = time.time(), None
        self.tracks = {}           # для скачивания: состояние каждого трека
        self.params = {}
        self.rerun = False

    def set_progress(self, frac, stage=None):
        self.progress = max(0.0, min(1.0, frac))
        if stage:
            self.stage = stage

    def to_dict(self):
        return {"id": self.id, "kind": self.kind, "slug": self.slug, "state": self.state,
                "progress": round(self.progress, 4), "stage": self.stage, "log": list(self.log)[-200:],
                "result": self.result, "error": self.error, "tracks": self.tracks,
                "started": self.started, "finished": self.finished}


class Jobs:
    def __init__(self):
        self.jobs = {}
        self.lock = threading.Lock()

    def running(self, slug, kind):
        with self.lock:
            return next((j for j in self.jobs.values()
                         if j.slug == slug and j.kind == kind and j.state == "running"), None)

    def start(self, kind, slug, fn, params=None):
        with self.lock:
            job = Job(kind, slug)
            job.params = params or {}
            self.jobs[job.id] = job
            finished = sorted((j for j in self.jobs.values() if j.state != "running"), key=lambda j: j.started)
            for old in finished[:-30]:
                self.jobs.pop(old.id, None)
        threading.Thread(target=self._run, args=(job, fn), daemon=True).start()
        return job

    def _run(self, job, fn):
        try:
            job.result = fn(job)
            job.state = "done"
            job.progress = 1.0
        except (q.Cancelled, youtube.Cancelled):
            job.state = "cancelled"
            job.stage = "Остановлено"
        except q.QuizError as e:
            job.state, job.error = "error", str(e)
        except Exception as e:  # неожиданная ошибка — показываем и пишем подробности в журнал
            job.state, job.error = "error", f"{type(e).__name__}: {e}"
            job.log.append(traceback.format_exc())
        finally:
            job.finished = time.time()

    def for_project(self, slug):
        with self.lock:
            items = [j for j in self.jobs.values() if j.slug == slug]
        latest = {}
        for j in sorted(items, key=lambda j: j.started):
            latest[j.kind] = j
        return {k: j.to_dict() for k, j in latest.items()}


# ───────────────────────── действия ─────────────────────────

class App:
    def __init__(self, ws):
        self.ws = ws
        self.jobs = Jobs()

    def tools(self):
        info = dict(youtube.tool_info())
        info["ffmpeg"] = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
        return info

    def track_status(self, project, slug):
        dl = self.jobs.running(slug, "download")
        live = dl.tracks if dl else {}
        out = {}
        for t in project["tracks"]:
            path = self.ws.resolve(t.get("file"))
            if t["id"] in live and live[t["id"]]["state"] in ("queued", "downloading"):
                st = dict(live[t["id"]])
            elif path and path.is_file():
                st = {"state": "ready"}
            elif t.get("error"):
                st = {"state": "error", "error": t["error"]}
            elif t.get("url"):
                st = {"state": "pending"}
            else:
                st = {"state": "missing", "error": "файл не найден"}
            out[t["id"]] = st
        return out

    def project_payload(self, slug):
        project = self.ws.load(slug)
        out_dir = self.ws.project_dir(slug) / "output"
        outputs = []
        if out_dir.is_dir():
            for p in sorted(out_dir.glob("*.mp4"), key=lambda p: -p.stat().st_mtime):
                if p.name.endswith(".part.mp4"):
                    continue
                desc = p.with_name(p.stem + "_description.txt")
                thumb = p.with_name(p.stem + "_thumbnail.jpg")
                outputs.append({"video": self.ws.rel(p), "size": p.stat().st_size, "time": p.stat().st_mtime,
                                "thumbnail": self.ws.rel(thumb) if thumb.exists() else None,
                                "description_text": desc.read_text(encoding="utf-8") if desc.exists() else ""})
        return {"slug": slug, "project": project, "status": self.track_status(project, slug),
                "clips": sc.clip_lengths(project),
                "jobs": self.jobs.for_project(slug), "outputs": outputs,
                "library": {"font": self.ws.library_files("font"), "image": self.ws.library_files("image"),
                            "music": self.ws.library_files("music")}}

    # --- задачи ---

    def start_download(self, slug, ids=None):
        running = self.jobs.running(slug, "download")
        if running:
            return running

        def run(job):
            result = q.download_tracks(self.ws, slug, ids, log=job.log.append, states=job.tracks,
                                       progress=job.set_progress, cancel=job.cancel)
            job.stage = f"Скачано {result['downloaded']}" + (f", ошибок {result['failed']}" if result["failed"] else "")
            return result

        return self.jobs.start("download", slug, run)

    def start_covers(self, slug, ids=None, forget=False):
        def run(job):
            return q.find_covers(self.ws, slug, set(ids) if ids else None, log=job.log.append,
                                 progress=job.set_progress, cancel=job.cancel, forget=forget)
        return self.jobs.start("covers", slug, run)

    def start_preview(self, slug, track_id=None):
        running = self.jobs.running(slug, "preview")
        if running:  # уже рисуется — перерисуем ещё раз с новыми настройками, когда закончит
            running.rerun = True
            running.params["track_id"] = track_id or running.params.get("track_id")
            return running

        def run(job):
            while True:
                job.rerun = False
                job.set_progress(0.1, "Рисую кадры")
                result = q.preview(self.ws, slug, job.params.get("track_id"), log=job.log.append)
                result["version"] = int(time.time() * 1000)
                if not job.rerun:
                    return result
        return self.jobs.start("preview", slug, run, {"track_id": track_id})

    def start_build(self, slug, limit=None, round_id=None, narration="voiced"):
        if self.jobs.running(slug, "build"):
            raise q.QuizError("Сборка уже идёт")

        def run(job):
            return q.build(self.ws, slug, limit=limit, log=job.log.append, progress=job.set_progress,
                           cancel=job.cancel, round_id=round_id, narration=narration)
        return self.jobs.start("build", slug, run, {"limit": limit, "round": round_id, "narration": narration})

    def start_sheet(self, slug):
        def run(job):
            job.set_progress(0.1, "Ищу обложки и рисую ответы")
            return {"image": q.answers_sheet(self.ws, slug, log=job.log.append), "version": int(time.time() * 1000)}
        return self.jobs.start("sheet", slug, run)


# ───────────────────────── HTTP ─────────────────────────

ROUTES = []


def route(method, pattern):
    def deco(fn):
        ROUTES.append((method, re.compile(f"^{pattern}$"), fn))
        return fn
    return deco


class Handler(BaseHTTPRequestHandler):
    server_version = "QuizMaker/2"
    protocol_version = "HTTP/1.1"
    app: App = None

    def log_message(self, fmt, *args):
        pass

    # --- ответы ---
    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status, message):
        self.send_json({"error": message}, status)

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise q.QuizError("Не понимаю запрос")

    # --- маршрутизация ---
    def handle_method(self, method):
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost", "[::1]"):
            return self.send_error_json(403, "Конструктор открывается только с этого компьютера")
        if method != "GET" and self.headers.get("X-Quiz") != "1":
            return self.send_error_json(403, "Запрос не из конструктора")
        parsed = urllib.parse.urlsplit(self.path)
        self.query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        path = urllib.parse.unquote(parsed.path)
        for m, rx, fn in ROUTES:
            if m == method:
                match = rx.match(path)
                if match:
                    try:
                        return fn(self, *match.groups())
                    except q.QuizError as e:
                        return self.send_error_json(400, str(e))
                    except (BrokenPipeError, ConnectionResetError):
                        return
                    except Exception as e:
                        traceback.print_exc()
                        return self.send_error_json(500, f"{type(e).__name__}: {e}")
        self.send_error_json(404, "Нет такой страницы")

    def do_GET(self):
        self.handle_method("GET")

    def do_POST(self):
        self.handle_method("POST")

    def do_PATCH(self):
        self.handle_method("PATCH")

    def do_PUT(self):
        self.handle_method("PUT")

    def do_DELETE(self):
        self.handle_method("DELETE")

    # --- файлы ---
    def serve_file(self, path, cache="no-cache"):
        try:
            size = path.stat().st_size
        except OSError:
            return self.send_error_json(404, "Файл не найден")
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        start, end = 0, size - 1
        status = 200
        rng = self.headers.get("Range")
        m = re.match(r"bytes=(\d*)-(\d*)$", rng or "")
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 206
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", cache)
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if self.query.get("download"):
            name = urllib.parse.quote(path.name)
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{name}")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(256 * 1024, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    @property
    def ws(self):
        return self.app.ws


# --- страница ---

@route("GET", "/")
def page(h):
    h.serve_file(WEB_DIR / "index.html")


@route("GET", "/media/(.+)")
def media(h, rel):
    root = str(h.ws.root)
    full = os.path.normpath(os.path.join(root, rel))
    allowed = [os.path.join(root, "library") + os.sep, os.path.join(root, "projects") + os.sep]
    if not any(full.startswith(a) for a in allowed):
        return h.send_error_json(403, "Нет доступа")
    h.serve_file(Path(full))


# --- общее ---

@route("GET", "/api/state")
def state(h):
    h.send_json({"projects": h.ws.list_projects(), "tools": None, "settings": h.ws.settings(),
                 "placeholders": q.TEXT_PLACEHOLDERS, "defaults": q.DEFAULT_PROJECT, "voices": q.VOICES,
                 "viz": config_schema(), "root": str(h.ws.root)})


@route("GET", "/api/tools")
def tools(h):
    if h.query.get("force"):
        youtube.tool_info(force=True)
    h.send_json(h.app.tools())


@route("PUT", "/api/settings")
def settings(h):
    data = h.read_json()
    try:
        shlex.split(data.get("ytdlp_args") or "")
    except ValueError:
        raise q.QuizError("Не понимаю параметры yt-dlp: проверьте кавычки")
    h.send_json(h.ws.save_settings(data))


@route("POST", "/api/upload")
def upload(h):
    kind, name = h.query.get("kind", ""), h.query.get("name", "")
    length = int(h.headers.get("Content-Length") or 0)
    if not length:
        raise q.QuizError("Пустой файл")
    if length > MAX_UPLOAD:
        raise q.QuizError("Файл больше 1 ГБ")
    rel = h.ws.save_upload(kind, name, h.rfile, length)
    result = {"path": rel}
    if kind == "font":
        result["styles"] = q.font_styles(h.ws.resolve(rel))
    slug = h.query.get("project")
    if kind == "audio" and slug:
        meta = q.local_meta(h.ws.resolve(rel))
        h.ws.add_tracks(slug, [{"file": rel, **{k: meta[k] for k in ("title", "artist", "album", "year")},
                                "duration": meta["duration"], "round": h.query.get("round", "")}])
    h.send_json(result)


@route("GET", "/api/font-styles")
def font_styles(h):
    path = h.ws.resolve(h.query.get("path", ""))
    h.send_json({"styles": q.font_styles(path) if path and path.is_file() else []})


@route("GET", "/api/waveform")
def wave(h):
    path = h.ws.resolve(h.query.get("file", ""))
    if not path or not path.is_file():
        raise q.QuizError("Трек ещё не скачан")
    h.send_json(q.waveform(h.ws, path))


# --- проекты ---

@route("POST", "/api/projects")
def create_project(h):
    data = h.read_json()
    name = (data.get("name") or "").strip() or "Новый квиз"
    slug = h.ws.duplicate(data["from"], name) if data.get("from") else h.ws.create(name)
    h.send_json(h.app.project_payload(slug))


@route("GET", "/api/projects/([\\w-]+)")
def get_project(h, slug):
    h.send_json(h.app.project_payload(slug))


@route("PATCH", "/api/projects/([\\w-]+)")
def patch_project(h, slug):
    data = h.read_json()
    h.ws.patch(slug, data.get("set"), data.get("unset"))
    h.send_json({"ok": True, "clips": sc.clip_lengths(h.ws.load(slug))})  # время раунда меняет окна на волне


@route("POST", "/api/projects/([\\w-]+)/tracks")
def add_tracks(h, slug):
    data = h.read_json()
    tracks, errors = q.parse_track_list(data.get("text") or "", h.ws)
    for t in tracks:
        t["round"] = data.get("round") or ""
    if tracks:
        h.ws.add_tracks(slug, tracks)
        if data.get("download", True) and any(t.get("url") for t in tracks):
            h.app.start_download(slug)
    payload = h.app.project_payload(slug)
    payload.update(added=len(tracks), errors=errors)
    h.send_json(payload)


@route("PATCH", "/api/projects/([\\w-]+)/tracks/([\\w]+)")
def patch_track(h, slug, tid):
    data = h.read_json()
    fields_ = dict(data)
    if "url" in fields_:  # новая ссылка — старый файл больше не подходит
        fields_.update(file="", thumb="", duration=None, error="", cover_auto="", cover_source="")
    project = h.ws.patch_track(slug, tid, fields_, internal="url" in data)
    track = next(t for t in project["tracks"] if t["id"] == tid)
    h.send_json({"track": track, "status": h.app.track_status(project, slug)[tid],
                 "clips": sc.clip_lengths(h.ws.load(slug))})


@route("DELETE", "/api/projects/([\\w-]+)/tracks/([\\w]+)")
def delete_track(h, slug, tid):
    h.ws.remove_track(slug, tid)
    h.send_json({"ok": True})


@route("POST", "/api/projects/([\\w-]+)/order")
def reorder(h, slug):
    h.ws.reorder(slug, h.read_json().get("ids") or [])
    h.send_json({"ok": True})


@route("POST", "/api/projects/([\\w-]+)/(download|covers|preview|build|sheet)")
def start_job(h, slug, kind):
    data = h.read_json()
    app = h.app
    if kind == "download":
        job = app.start_download(slug, data.get("ids"))
    elif kind == "covers":
        job = app.start_covers(slug, data.get("ids"), bool(data.get("forget")))
    elif kind == "preview":
        job = app.start_preview(slug, data.get("track_id"))
    elif kind == "sheet":
        job = app.start_sheet(slug)
    else:
        limit = data.get("limit")
        job = app.start_build(slug, int(limit) if limit else None, data.get("round") or None,
                              "silent" if data.get("voice") is False else "voiced")
    h.send_json(job.to_dict())


@route("GET", "/api/jobs/(\\w+)")
def get_job(h, jid):
    job = h.app.jobs.jobs.get(jid)
    if not job:
        return h.send_error_json(404, "Задача не найдена")
    h.send_json(job.to_dict())


@route("POST", "/api/jobs/(\\w+)/cancel")
def cancel_job(h, jid):
    job = h.app.jobs.jobs.get(jid)
    if job:
        job.cancel.set()
    h.send_json({"ok": True})


# ───────────────────────── запуск ─────────────────────────

class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        import sys
        if isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError, TimeoutError)):
            return  # браузер прервал загрузку (например, перемотал трек) — это нормально
        super().handle_error(request, client_address)


def serve(root=None, port=8765, open_browser=True):
    ws = q.Workspace(root or q.ROOT)
    (ws.projects).mkdir(parents=True, exist_ok=True)
    (ws.library).mkdir(parents=True, exist_ok=True)
    Handler.app = App(ws)
    threading.Thread(target=youtube.tool_info, daemon=True).start()  # проверка yt-dlp — заранее, в фоне
    server = None
    for p in range(port, port + 20):
        try:
            server = Server(("127.0.0.1", p), Handler)
            break
        except OSError:
            continue
    if server is None:
        raise SystemExit(f"Порты {port}–{port + 19} заняты. Укажите другой: --port 9000")
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"Конструктор квизов: {url}")
    print(f"Рабочая папка: {ws.root}")
    print("Остановить: Ctrl+C")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено.")
    finally:
        server.server_close()


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Веб-конструктор квизов")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--home", type=Path, help="рабочая папка (по умолчанию — папка со скриптами)")
    a = ap.parse_args()
    serve(a.home, a.port, not a.no_browser)
