#!/usr/bin/env python3
"""
make_quiz.py — конструктор видео-квизов «Угадай песню».

Основной способ — веб-конструктор в браузере:
    uv run make_quiz.py serve

То же самое из терминала:
    uv run make_quiz.py new "Угадай хит 2000-х" --links ссылки.txt   # создать квиз со списком треков
    uv run make_quiz.py list                                          # какие квизы есть
    uv run make_quiz.py add ugadai-khit-2000-kh ещё.txt               # добавить треки
    uv run make_quiz.py download ugadai-khit-2000-kh                  # скачать треки и найти обложки
    uv run make_quiz.py preview ugadai-khit-2000-kh                   # кадры всех экранов и лист ответов
    uv run make_quiz.py build ugadai-khit-2000-kh --limit 2           # пробный ролик из 2 песен
    uv run make_quiz.py build ugadai-khit-2000-kh                     # весь квиз

Файл со ссылками — по треку в строке: «ссылка | начало | название | исполнитель | подсказка»,
всё кроме ссылки — по желанию. Подходит и старая таблица quiz.csv (file,start,title,artist,…).
Оформление, тексты и визуализатор хранятся в projects/<имя>/project.json.

Установка (один раз, в папке конструктора):
    sudo apt install ffmpeg
    uv sync
"""

import argparse
import shutil
import sys
from pathlib import Path

try:
    import numpy  # noqa: F401
    from PIL import Image  # noqa: F401
except ImportError as e:
    sys.exit(f"Не хватает библиотеки ({e.name}). Выполните «uv sync» в папке конструктора "
             f"и запускайте его так: uv run make_quiz.py serve")

import quiz_core as q


_bar = {"shown": False, "last": None}


def progress_bar(frac, stage):
    width = 28
    filled = int(width * frac)
    line = f"  [{'█' * filled}{'·' * (width - filled)}] {frac * 100:3.0f}%  {stage[:50]}"
    if line == _bar["last"]:
        return
    _bar.update(shown=True, last=line)
    sys.stdout.write("\r\033[K" + line)
    sys.stdout.flush()
    if frac >= 1:
        sys.stdout.write("\n")
        _bar.update(shown=False, last=None)


def say(msg):
    """Сообщение поверх полосы прогресса: стираем полосу, печатаем, полоса появится снова."""
    if _bar["shown"]:
        sys.stdout.write("\r\033[K")
        _bar["last"] = None
    print(msg, flush=True)


def find_slug(ws, name):
    p = Path(name)
    if (p / "project.json").is_file():
        return p.name if p.absolute().parent == ws.projects.absolute() else sys.exit(
            f"Проект должен лежать в {ws.projects}")
    slugs = [x["slug"] for x in ws.list_projects()]
    if name in slugs:
        return name
    by_name = [x["slug"] for x in ws.list_projects() if x["name"] == name]
    if len(by_name) == 1:
        return by_name[0]
    sys.exit(f"Нет квиза «{name}». Есть: {', '.join(slugs) or 'пока ни одного'}")


def add_from_file(ws, slug, path):
    path = Path(path)
    if not path.is_file():
        sys.exit(f"Нет файла {path}")
    tracks, errors = q.parse_track_list(path.read_text(encoding="utf-8-sig"), ws, base_dir=path.parent)
    for e in errors:
        print(f"  пропущено: {e}")
    if tracks:
        ws.add_tracks(slug, tracks)
    print(f"Добавлено треков: {len(tracks)}")
    return len(tracks)


def main():
    ap = argparse.ArgumentParser(description="Конструктор видео-квизов «Угадай песню».")
    ap.add_argument("--home", type=Path, help="рабочая папка (по умолчанию — папка со скриптами)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="открыть веб-конструктор")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--no-browser", action="store_true", help="не открывать браузер")

    sub.add_parser("list", help="список квизов")

    s = sub.add_parser("new", help="создать квиз")
    s.add_argument("name", help="название")
    s.add_argument("--links", type=Path, help="файл со ссылками или таблица CSV")

    s = sub.add_parser("add", help="добавить треки из файла")
    s.add_argument("project")
    s.add_argument("file", type=Path)

    for name, help_ in (("download", "скачать треки и найти обложки"), ("covers", "найти обложки заново"),
                        ("preview", "кадры всех экранов и лист ответов")):
        s = sub.add_parser(name, help=help_)
        s.add_argument("project")

    s = sub.add_parser("build", help="собрать видео")
    s.add_argument("project")
    s.add_argument("--limit", type=int, help="только первые N песен — пробный ролик")
    s.add_argument("--round", help="только этот тур (название или id) — пробный ролик")
    s.add_argument("--no-download", action="store_true", help="не докачивать треки перед сборкой")

    a = ap.parse_args()
    ws = q.Workspace(a.home or q.ROOT)

    if a.cmd == "serve":
        import server
        server.serve(ws.root, a.port, not a.no_browser)
        return

    if a.cmd != "list" and a.cmd != "new" and not shutil.which("ffmpeg"):
        sys.exit("Не найден ffmpeg. Установите: sudo apt install ffmpeg")

    try:
        if a.cmd == "list":
            projects = ws.list_projects()
            if not projects:
                print("Квизов пока нет. Создайте: uv run make_quiz.py new \"Название\" --links ссылки.txt")
            for p in projects:
                print(f"  {p['slug']:<32} {p['name']}  ({p['tracks']} {q.plural(p['tracks'], 'трек', 'трека', 'треков')})")

        elif a.cmd == "new":
            slug = ws.create(a.name)
            print(f"Создан квиз «{a.name}»: {ws.rel(ws.project_dir(slug))}")
            if a.links and add_from_file(ws, slug, a.links):
                q.download_tracks(ws, slug, log=say, progress=progress_bar)
            print(f"Дальше: uv run make_quiz.py build {slug} --limit 2")

        elif a.cmd == "add":
            slug = find_slug(ws, a.project)
            if add_from_file(ws, slug, a.file):
                q.download_tracks(ws, slug, log=say, progress=progress_bar)

        elif a.cmd == "download":
            slug = find_slug(ws, a.project)
            r = q.download_tracks(ws, slug, log=say, progress=progress_bar)
            print(f"Скачано: {r['downloaded']}" + (f", с ошибкой: {r['failed']}" if r["failed"] else ""))

        elif a.cmd == "covers":
            slug = find_slug(ws, a.project)
            q.find_covers(ws, slug, log=say)

        elif a.cmd == "preview":
            slug = find_slug(ws, a.project)
            r = q.preview(ws, slug, log=say)
            for name, path in r["images"].items():
                print(f"  {name:<9} {path}")
            q.answers_sheet(ws, slug, log=say)

        elif a.cmd == "build":
            slug = find_slug(ws, a.project)
            if not a.no_download:
                project = ws.load(slug)
                if any(t.get("url") and not (ws.resolve(t.get("file")) and ws.resolve(t["file"]).is_file())
                       for t in project["tracks"] if t.get("enabled", True)):
                    q.download_tracks(ws, slug, log=say, progress=progress_bar)
            r = q.build(ws, slug, limit=a.limit, log=say, progress=progress_bar, round_id=a.round)
            print(f"\n  видео     {ws.resolve(r['video'])}")
            print(f"  обложка   {ws.resolve(r['thumbnail'])}")
            print(f"  описание  {ws.resolve(r['description'])}")
    except q.QuizError as e:
        sys.exit(f"\n{e}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nОстановлено.")
