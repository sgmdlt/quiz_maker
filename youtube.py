"""
youtube.py — скачивание треков по ссылкам через yt-dlp.

Скачанное хранится в library/youtube/<id видео>.<ext> вместе с <id>.info.json
и миниатюрой <id>.jpg, поэтому один и тот же трек в разных квизах скачивается один раз.
Начало отрывка берётся из ссылки, если в ней есть время (…?t=65 или &t=1m5s) —
на YouTube это «Поделиться → Начало: 1:05».

Название, исполнитель, альбом и год берутся из данных видео: у треков из YouTube Music
они точные, у клипов разбирается заголовок вида «Исполнитель - Песня (Official Video)».

yt-dlp и JavaScript-движок deno (без него YouTube отдаёт не всё) ставятся вместе с проектом:
    uv sync
и находятся, когда конструктор запущен через «uv run make_quiz.py …».
Обновление (YouTube часто что-то меняет):
    uv sync -P yt-dlp -P yt-dlp-ejs -P deno
"""

INSTALL_HINT = "выполните «uv sync» в папке конструктора и запускайте его через «uv run make_quiz.py serve»"
UPDATE_HINT = "uv sync -P yt-dlp -P yt-dlp-ejs -P deno"

import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

AUDIO_EXTS = {".webm", ".m4a", ".mp3", ".opus", ".ogg", ".oga", ".aac", ".flac", ".wav", ".mp4", ".mka", ".mkv"}


class DownloadError(Exception):
    pass


class Cancelled(Exception):
    pass


# ───────────────────────── инструменты ─────────────────────────

def ytdlp_command():
    exe = shutil.which("yt-dlp")
    if exe:
        return [exe]
    try:
        import yt_dlp  # noqa: F401
        return [sys.executable, "-m", "yt_dlp"]
    except ImportError:
        return None


_tool_cache = {"t": 0, "info": None}


def tool_info(force=False):
    """Версия yt-dlp и найденный JavaScript-движок. Кэшируется на минуту."""
    if not force and _tool_cache["info"] and time.time() - _tool_cache["t"] < 60:
        return _tool_cache["info"]
    info = {"ytdlp": None, "js_runtime": None, "problem": None}
    cmd = ytdlp_command()
    if not cmd:
        info["problem"] = "yt-dlp не найден"
    else:
        try:
            r = subprocess.run(cmd + ["--version"], capture_output=True, text=True, timeout=30)
            info["ytdlp"] = r.stdout.strip() or None
            r = subprocess.run(cmd + ["--ignore-config", "-v", "--simulate", "about:blank"],
                               capture_output=True, text=True, timeout=30)
            m = re.search(r"JS runtimes:\s*(.+)", r.stdout + r.stderr)
            runtime = m.group(1).strip() if m else ""
            if runtime and runtime.lower() != "none":
                info["js_runtime"] = runtime
            else:
                info["problem"] = "нет JavaScript-движка для YouTube (deno)"
        except (OSError, subprocess.TimeoutExpired) as e:
            info["problem"] = f"yt-dlp не запускается: {e}"
    _tool_cache.update(t=time.time(), info=info)
    return info


# ───────────────────────── ссылки ─────────────────────────

def parse_time_param(value):
    """«65», «65s», «1m5s», «1h2m3s», «1:05» → секунды."""
    if not value:
        return None
    s = str(value).strip().lower()
    if re.fullmatch(r"\d+(\.\d+)?s?", s):
        return float(s.rstrip("s"))
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", s)
    if m and any(m.groups()):
        h, mi, se = (int(x or 0) for x in m.groups())
        return float(h * 3600 + mi * 60 + se)
    if re.fullmatch(r"\d+(:\d+){1,2}(\.\d+)?", s):
        total = 0.0
        for part in s.split(":"):
            total = total * 60 + float(part)
        return total
    return None


def parse_url(url):
    """→ (ключ для имени файла, время начала из ссылки или None, это YouTube?)"""
    url = url.strip()
    u = urllib.parse.urlparse(url)
    host = (u.hostname or "").lower()
    query = urllib.parse.parse_qs(u.query)
    frag = urllib.parse.parse_qs(u.fragment)
    vid = None
    if host.endswith("youtu.be"):
        vid = u.path.strip("/").split("/")[0]
    elif "youtube" in host:
        if u.path.rstrip("/") == "/watch":
            vid = (query.get("v") or [None])[0]
        else:
            m = re.match(r"/(?:shorts|live|embed|v)/([\w-]{11})", u.path)
            vid = m.group(1) if m else None
    start = None
    for key in ("t", "start", "time_continue"):
        val = (query.get(key) or frag.get(key) or [None])[0]
        start = parse_time_param(val)
        if start is not None:
            break
    if vid and re.fullmatch(r"[\w-]{11}", vid):
        return vid, start, True
    return "url-" + hashlib.sha1(url.encode()).hexdigest()[:12], start, False


def is_url(text):
    return bool(re.match(r"https?://\S+$", text.strip()))


# ───────────────────────── данные трека ─────────────────────────

NOISE = re.compile(
    r"\s*[\(\[]\s*(?:official|офиц|music\s*video|lyric|lyrics|audio|video|clip|клип|hd|hq|4k|"
    r"visuali[sz]er|премьера|текст|mv\b|m/v|full\s*version)[^\)\]]*[\)\]]", re.IGNORECASE)


def clean_title(text):
    t = NOISE.sub("", text or "")
    t = re.split(r"\s+[|│]\s+", t)[0]          # «Песня | Official Video»
    t = re.sub(r"\s+(?:official\s+(?:music\s+)?video|lyrics?|hd|4k)\s*$", "", t, flags=re.IGNORECASE)
    return t.strip(" -–—").strip()


def _unquote(s):
    return s.strip().strip("\"'«»“”„").strip()


def clean_channel(name):
    n = re.sub(r"\s*-\s*Topic$", "", name or "")
    n = re.sub(r"(?i)\s*(VEVO|official|официальный канал)$", "", n)
    return n.strip()


def track_meta(info):
    """Название, исполнитель, альбом, год и длительность из info.json yt-dlp."""
    title = info.get("track") or ""
    artists = info.get("artists") or ([info["artist"]] if info.get("artist") else [])
    artist = ", ".join(dict.fromkeys(a.strip() for a in artists if a and a.strip()))
    if not (title and artist):
        raw = clean_title(info.get("title") or info.get("fulltitle") or "")
        parts = re.split(r"\s+[-–—]\s+", raw, maxsplit=1)
        quoted = re.match(r"(.+?)\s*[«\"“„](.+?)[»\"”]\s*$", raw)  # Кино «Группа крови»
        if len(parts) == 2 and all(parts):
            artist = artist or _unquote(parts[0])
            title = title or _unquote(parts[1])
        elif quoted:
            artist = artist or _unquote(quoted.group(1))
            title = title or _unquote(quoted.group(2))
        else:
            title = title or _unquote(raw)
            artist = artist or clean_channel(info.get("channel") or info.get("uploader") or "")
    year = info.get("release_year") or (info.get("release_date") or "")[:4]
    return {
        "title": title.strip(),
        "artist": artist.strip(),
        "album": (info.get("album") or "").strip(),
        "year": str(year or ""),
        "duration": info.get("duration"),
    }


def _files_for(dest, key):
    audio = thumb = info = None
    for p in dest.glob(f"{key}.*"):
        name = p.name
        if name.endswith(".info.json"):
            info = p
        elif p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp") and not name.endswith(".part"):
            thumb = p if thumb is None or p.suffix == ".jpg" else thumb
        elif p.suffix.lower() in AUDIO_EXTS and not name.endswith((".part", ".ytdl")):
            audio = p
    return audio, thumb, info


def download_video(url, dest, extra_args=()):
    """Скачивает видео (до 1080p) в dest/<ключ>.mp4. Возвращает путь."""
    cmd = ytdlp_command()
    if not cmd:
        raise DownloadError(f"yt-dlp не найден: {INSTALL_HINT}")
    key, _, _ = parse_url(url)
    out = Path(dest) / f"{key}.mp4"
    if out.is_file():
        return out
    r = subprocess.run(cmd + ["-f", "bv*[height<=1080]+ba/b", "--merge-output-format", "mp4", "--no-playlist",
                              "-o", str(out), *extra_args, url], capture_output=True, text=True)
    if r.returncode != 0 or not out.is_file():
        raise DownloadError(friendly_error(r.stdout.splitlines() + r.stderr.splitlines(), True))
    return out


def find_cached(dest, url):
    key, _, _ = parse_url(url)
    audio, thumb, info = _files_for(Path(dest), key)
    if not audio:
        return None
    return _result(key, audio, thumb, info)


def _result(key, audio, thumb, info_path):
    info = {}
    if info_path:
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            info = {}
    meta = track_meta(info) if info else {"title": audio.stem, "artist": "", "album": "", "year": "", "duration": None}
    return {"key": key, "audio": audio, "thumb": thumb, **meta}


# ───────────────────────── скачивание ─────────────────────────

def friendly_error(lines, has_js):
    text = "\n".join(lines)
    errors = [l for l in lines if l.startswith("ERROR")]
    last = re.sub(r"^ERROR:\s*(\[[^\]]+\]\s*[\w-]+:\s*)?", "", errors[-1]) if errors else (lines[-1] if lines else "")
    low = text.lower()
    if "sign in to confirm" in low or "not a bot" in low:
        return ("YouTube просит подтвердить, что вы не бот. В настройках скачивания добавьте "
                "«--cookies-from-browser firefox» (или chrome) — yt-dlp возьмёт вход из браузера.")
    if "private video" in low:
        return "Видео закрыто владельцем."
    if "not available in your country" in low or "geo restrict" in low:
        return "Видео недоступно в вашей стране."
    if "video unavailable" in low or "has been removed" in low:
        return "Видео недоступно или удалено."
    if "confirm your age" in low or "age-restricted" in low:
        return "Видео с ограничением по возрасту — нужен вход: добавьте «--cookies-from-browser firefox»."
    if "unsupported url" in low:
        return "Эта ссылка не поддерживается."
    if ("requested format is not available" in low or "only images are available" in low) and not has_js:
        return f"YouTube не отдал звук: yt-dlp не нашёл deno — {INSTALL_HINT}."
    if "http error 404" in low:
        return "По ссылке ничего нет (ошибка 404)."
    if "http error 403" in low and "proxy" not in low:
        return f"Сайт запретил скачивание (ошибка 403). Обновите yt-dlp: {UPDATE_HINT}"
    if any(s in low for s in ("unable to download", "connection", "timed out", "name resolution")):
        return "Нет связи с сайтом. Проверьте интернет. " + last[:200]
    return last[:400] or "yt-dlp завершился с ошибкой"


def download(url, dest, on_progress=None, cancel=None, extra_args=(), has_js=True):
    """Скачивает звук по ссылке в папку dest. Возвращает dict: key, audio, thumb, title, artist, …"""
    cmd = ytdlp_command()
    if not cmd:
        raise DownloadError(f"yt-dlp не найден: {INSTALL_HINT}")
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    cached = find_cached(dest, url)
    if cached:
        return cached
    key, _, _ = parse_url(url)
    args = cmd + [
        "-f", "bestaudio/best", "--no-playlist", "--newline", "--no-colors",
        "-o", str(dest / f"{key}.%(ext)s"),
        "--write-info-json", "--write-thumbnail", "--convert-thumbnails", "jpg",
        *extra_args, url,
    ]
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    lines = []
    try:
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            lines.append(line)
            del lines[:-40]
            m = re.search(r"\[download\]\s+([\d.]+)%", line)
            if m and on_progress:
                on_progress(min(1.0, float(m.group(1)) / 100))
            if cancel is not None and cancel.is_set():
                proc.kill()
                raise Cancelled()
        rc = proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
    if cancel is not None and cancel.is_set():
        raise Cancelled()
    audio, thumb, info = _files_for(dest, key)
    if rc != 0 or not audio:
        raise DownloadError(friendly_error(lines, has_js))
    return _result(key, audio, thumb, info)
