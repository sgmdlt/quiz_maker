#!/usr/bin/env python3
"""
cover.py — конструктор обложки квиза для соцсетей и превью YouTube (1920×1080).

    uv run cover.py <квиз> [--port 8790] [--no-browser]

Python раскладывает обложки квиза радугой (столбцы по оттенку, серые справа, сетка выходит
за края кадра) и отдаёт страницу web/cover.html. Логотип, тексты и засвет рисуются в браузере
на canvas. Настройки страницы сохраняются в projects/<квиз>/cover.json, шрифты — в library/fonts.
"""

import argparse
import json
import math
import mimetypes
import re
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
from PIL import Image

import quiz_core as qc

W, H = 1920, 1080
WEB = Path(__file__).resolve().parent / "web" / "cover.html"
FONT_EXT = (".ttf", ".otf", ".woff", ".woff2")
HUE_START = math.radians(20)  # радуга начинается с красного
GREY_CHROMA = 12  # ниже — обложка считается серой и уходит в правые столбцы

for ext, mime in {".ttf": "font/ttf", ".otf": "font/otf", ".webp": "image/webp"}.items():
    mimetypes.add_type(mime, ext)


# ───────────────────────── какие обложки ─────────────────────────


def track_cover(ws, track):
    for key in ("cover", "cover_auto", "thumb"):
        path = ws.resolve(track.get(key))
        if path and path.is_file():
            return path


def quiz_covers(ws, project):
    """Обложки квиза по порядку туров, без повторов; в «Трёх попытках» — одна на исполнителя."""
    out = []
    for rd in project["rounds"]:
        tracks = [t for t in project["tracks"] if t["round"] == rd["id"] and t.get("enabled", True)]
        for t in tracks[::3] if rd.get("kind") == "three_tries" else tracks:
            path = track_cover(ws, t)
            if path and path not in out:
                out.append(path)
    return out


# ───────────────────────── цвет и раскладка ─────────────────────────


def srgb_to_lab(rgb):
    """rgb: массив (..., 3) в 0..1 → Lab."""
    c = np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)
    m = np.array([[0.4124, 0.3576, 0.1805], [0.2126, 0.7152, 0.0722], [0.0193, 0.1192, 0.9505]])
    xyz = c @ m.T / np.array([0.9505, 1.0, 1.089])
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16 / 116)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])], -1)


def mean_lab(path):
    """Средний цвет обложки — так она выглядит издалека."""
    a = np.asarray(Image.open(path).convert("RGB").resize((24, 24)), dtype=float) / 255
    return srgb_to_lab(a).reshape(-1, 3).mean(0)


def grid_for(n, width=W, height=H):
    """Сетка cols×rows ≤ n, выходящая за кадр на 0.15…0.45 плитки с каждой стороны."""
    best = None
    for rows in range(2, 15):
        for cols in range(rows, 30):
            if cols * rows > n:
                break
            tile = max(width / (cols - 0.3), height / (rows - 0.3))
            side_x, side_y = (cols * tile - width) / tile / 2, (rows * tile - height) / tile / 2
            if max(side_x, side_y) <= 0.45 and (best is None or cols * rows > best[0] * best[1]):
                best = (cols, rows, math.ceil(tile))
    return best


def rainbow_layout(paths, cols, rows):
    """Радуга по столбцам: оттенок слева направо, серые справа. Внутри столбца — цепочка
    от самой светлой обложки к ближайшей по цвету. Лишние (самые серые) не берутся.
    Возвращает пути по строкам."""
    lab = {p: mean_lab(p) for p in paths}
    chroma = {p: math.hypot(lab[p][1], lab[p][2]) for p in paths}
    chosen = sorted(paths, key=lambda p: -chroma[p])[: cols * rows]

    def hue_key(p):
        L, a, b = lab[p]
        return chroma[p] < GREY_CHROMA, (math.atan2(b, a) - HUE_START) % (2 * math.pi), -L

    ordered = sorted(chosen, key=hue_key)
    grid = [None] * (cols * rows)
    for c in range(cols):
        rest = ordered[c * rows : (c + 1) * rows]
        chain = [max(rest, key=lambda p: lab[p][0])]
        rest.remove(chain[0])
        while rest:
            nxt = min(rest, key=lambda p: np.linalg.norm(lab[p] - lab[chain[-1]]))
            chain.append(nxt)
            rest.remove(nxt)
        for r, p in enumerate(chain):
            grid[r * cols + c] = p
    return grid


# ───────────────────────── сервер страницы ─────────────────────────


class App:
    def __init__(self, ws, slug):
        self.ws, self.slug = ws, slug
        project = ws.load(slug)
        paths = quiz_covers(ws, project)
        cols, rows, tile = grid_for(len(paths))
        self.covers = rainbow_layout(paths, cols, rows)
        self.layout = {"width": W, "height": H, "cols": cols, "rows": rows, "tile": tile}
        self.settings_path = ws.project_dir(slug) / "cover.json"
        self.fonts_dir = ws.library / "fonts"
        self.defaults = {
            "theme": (project["texts"].get("intro_title") or project["name"]).replace("№", "#"),
            "style": {k: project["style"][k] for k in ("bg_top", "bg_bottom", "text")},
            "logo_colors": list(project["visualizer"].get("colors") or ["#FFBB00", "#D16500"]),
        }
        print(f"Обложек: {len(paths)} → сетка {cols}×{rows}, плитка {tile}px")

    def info(self):
        settings = json.loads(self.settings_path.read_text("utf-8")) if self.settings_path.is_file() else {}
        fonts = sorted(p.name for p in self.fonts_dir.glob("*") if p.suffix.lower() in FONT_EXT)
        return {
            "slug": self.slug,
            "layout": self.layout,
            "covers": len(self.covers),
            "fonts": fonts,
            "defaults": self.defaults,
            "settings": settings,
        }


class Handler(BaseHTTPRequestHandler):
    app: App = None

    def log_message(self, *args):
        pass

    def send(self, body, ctype, status=200):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, data, status=200):
        self.send(json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8", status)

    def send_path(self, path):
        if not path or not path.is_file():
            return self.send_json({"error": "Нет такого файла"}, 404)
        self.send(path.read_bytes(), mimetypes.guess_type(path.name)[0] or "application/octet-stream")

    def body(self):
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def guard(self, method):
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            return False
        return method == "GET" or self.headers.get("X-Quiz") == "1"

    def do_GET(self):
        if not self.guard("GET"):
            return self.send_json({"error": "Только с этого компьютера"}, 403)
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        app = self.app
        if path == "/":
            return self.send_path(WEB)
        if path == "/api/info":
            return self.send_json(app.info())
        if m := re.fullmatch(r"/cover/(\d+)", path):
            i = int(m[1])
            return self.send_path(app.covers[i] if i < len(app.covers) else None)
        if m := re.fullmatch(r"/fonts/([^/]+)", path):
            return self.send_path(app.fonts_dir / m[1])
        self.send_json({"error": "Нет такой страницы"}, 404)

    def do_PUT(self):
        if not self.guard("PUT"):
            return self.send_json({"error": "Запрос не со страницы обложки"}, 403)
        path = urllib.parse.urlsplit(self.path).path
        app = self.app
        if path == "/api/settings":
            app.settings_path.write_text(self.body().decode("utf-8"), "utf-8")
            return self.send_json({"ok": True})
        if path == "/api/cover":  # первый экран ролика и значок YouTube (см. build)
            app.settings_path.with_name("cover.png").write_bytes(self.body())
            return self.send_json({"ok": True})
        if path == "/api/logo":  # логотип на заставках всех квизов
            (app.ws.library / "logo.png").write_bytes(self.body())
            app.ws.patch_theme({"style.logo": "library/logo.png"}, [])
            return self.send_json({"ok": True})
        if m := re.fullmatch(r"/fonts/([^/]+)", urllib.parse.unquote(path)):
            name = Path(m[1]).name
            if Path(name).suffix.lower() not in FONT_EXT:
                return self.send_json({"error": "Нужен .ttf, .otf, .woff или .woff2"}, 400)
            self.app.fonts_dir.mkdir(parents=True, exist_ok=True)
            (self.app.fonts_dir / name).write_bytes(self.body())
            return self.send_json({"ok": True, "name": name})
        self.send_json({"error": "Нет такой страницы"}, 404)


def main():
    ap = argparse.ArgumentParser(description="Конструктор обложки квиза")
    ap.add_argument("slug", help="папка квиза в projects/")
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--home", help="другой корень вместо папки со скриптами")
    args = ap.parse_args()
    Handler.app = App(qc.Workspace(args.home or qc.ROOT), args.slug)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Обложка квиза: {url}\nОстановить: Ctrl+C")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено.")


if __name__ == "__main__":
    main()
