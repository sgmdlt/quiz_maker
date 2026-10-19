"""
quiz_core.py — ядро конструктора: проекты квизов, оформление экранов, предпросмотр и сборка видео.

Устройство рабочей папки (по умолчанию — папка со скриптами, или переменная QUIZ_HOME):
    projects/<имя>/project.json   — квиз: треки, тексты, шрифты, цвета, визуализатор
    projects/<имя>/output/        — готовое видео, обложка ролика, описание, кадры предпросмотра
    library/youtube/              — скачанные треки (общие для всех квизов)
    library/fonts, images, covers, audio — загруженные шрифты, фоны, обложки, свои треки
    library/covers/auto/          — обложки, найденные автоматически
Пути в project.json записываются относительно рабочей папки.

Структура ролика:
    заставка → вопрос 1 … вопрос N → экран «Ответы» → ответ 1 … ответ N → финальный экран
"""

import copy
import csv
import hashlib
import io
import json
import math
import os
import random
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import wave
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageColor, ImageDraw, ImageFilter, ImageFont, ImageOps

from covers import CoverFinder
from spectrum_ring import SpectrumRing, analyze, config_from_dict

ROOT = Path(os.environ.get("QUIZ_HOME") or Path(__file__).resolve().parent)

FPS = 30
SAMPLE_RATE = 44100
SAMPLES_PER_FRAME = SAMPLE_RATE // FPS  # 1470 — звук и картинка совпадают до сэмпла
FADE_FRAMES = 8                          # плавный переход между экранами (~0.27 с)
LOUDNESS = -14                           # целевая громкость, LUFS (как у YouTube)
SPLIT_PAUSE = 2.0                        # пауза между двумя фрагментами вопроса, с


class QuizError(Exception):
    """Ошибка, которую нужно показать пользователю как есть."""


class Cancelled(QuizError):
    def __init__(self):
        super().__init__("Остановлено")


# ───────────────────────── проект по умолчанию ─────────────────────────

DEFAULT_PROJECT = {
    "version": 1,
    "name": "Новый квиз",
    "format": "horizontal",           # horizontal (1920x1080) | vertical (1080x1920, Shorts)
    "timing": {"question": 10, "answer": 6, "gap": 2, "intro": 3, "round": 6, "answers_card": 3, "outro": 4},
    "voice": "ru-RU-DmitryNeural",    # голос для заставок квиза и туров (VOICES), "" — без озвучки
    "music": "",                      # фоновая музыка заставок квиза и туров, "" — без музыки
    "music_volume": 0.3,              # громкость музыки относительно голоса, 0..1
    "answer_audio": "replay",         # replay — тот же отрывок | continue — продолжение | none — тишина
    "shuffle": False,
    "texts": {
        "intro_title": "Угадай песню",
        "intro_subtitle": "{songs} · {seconds} на каждую",
        "question_label": "Вопрос {n} из {total}",
        "prompt": "Что это за песня?",
        "answers_title": "Ответы · {round}",
        "answers_subtitle": "Проверьте себя",
        "answer_label": "Ответ {n} из {total}",
        "outro_title": "Сколько угадали?",
        "outro_subtitle": "Напишите свой счёт в комментариях",
        # текст для диктора: на заставке пусто — читается текст слайда, на «Ответах» и финале — тишина
        "intro_narration": "",
        "answers_narration": "",
        "outro_narration": "",
    },
    "fonts": {"heading": "", "text": ""},   # путь к шрифту, для вариативного — «путь#Bold»
    "style": {
        "bg_top": "#1B1035",
        "bg_bottom": "#4A1B5C",
        "glow": "#7B3FA0",
        "text": "#FFFFFF",
        "muted": "#CFC6E8",
        "accent": "#FFC83D",
        "background": "",             # картинка для фона вместо градиента
        "cover_backdrop": True,       # фон экрана ответа — размытая обложка
    },
    "visualizer": {},                 # только изменённые параметры RingConfig
    "covers": {"online": True, "itunes_country": "US"},
    "rounds": [],                     # [{id, title, rules}], у трека — id своего тура
    "tracks": [],
}

DEFAULT_TRACK = {
    "id": "",
    "round": "",            # id тура
    "enabled": True,
    "url": "",              # ссылка (YouTube или прямая на файл)
    "file": "",             # аудиофайл, когда скачан или загружен
    "thumb": "",            # миниатюра видео
    "duration": None,
    "start": 0.0,
    "start2": None,         # начало второго фрагмента: вопрос делится на два по половине времени
    "title": "",
    "artist": "",
    "album": "",
    "year": "",
    "hint": "",
    "question": "",         # свой вопрос, иначе texts.prompt
    "answer": "",           # свой ответ, иначе исполнитель
    "cover": "",            # своя обложка
    "cover_auto": "",       # найденная автоматически
    "cover_source": "",
    "error": "",
}

PATCHABLE = {"name", "format", "timing", "answer_audio", "shuffle", "texts", "fonts", "style", "visualizer", "covers",
             "rounds", "voice", "music", "music_volume"}
VOICES = {"ru-RU-DmitryNeural": "Дмитрий", "ru-RU-SvetlanaNeural": "Светлана",  # edge-tts, нужен интернет
          "ru_RU-dmitri-medium": "Дмитрий (Piper, без интернета)", "ru_RU-irina-medium": "Ирина (Piper, без интернета)",
          "ru_RU-denis-medium": "Денис (Piper, без интернета)", "ru_RU-ruslan-medium": "Руслан (Piper, без интернета)"}
TRACK_EDITABLE = {"round", "enabled", "url", "start", "start2", "title", "artist", "album", "year", "hint", "question", "answer", "cover"}

TEXT_PLACEHOLDERS = {
    "{n}": "номер вопроса", "{total}": "всего вопросов",
    "{songs}": "«20 песен»", "{seconds}": "«10 секунд»", "{count}": "число песен",
    "{round}": "название тура",
}


def deep_merge(base, extra):
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k != "visualizer":
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def new_track(**fields_):
    t = copy.deepcopy(DEFAULT_TRACK)
    t.update({k: v for k, v in fields_.items() if k in DEFAULT_TRACK})
    t["id"] = t["id"] or secrets.token_hex(4)
    return t


def new_round(n):
    # id постоянный: квиз без туров при каждом чтении получает один и тот же первый тур
    return {"id": f"r{n}", "title": f"Тур {n}", "rules": ""}


def normalize(project):
    p = deep_merge(DEFAULT_PROJECT, project)
    p["tracks"] = [new_track(**t) for t in p.get("tracks") or []]
    p["rounds"] = p["rounds"] or [new_round(1)]
    ids = {r["id"] for r in p["rounds"]}
    for t in p["tracks"]:  # трек без тура (или тур удалён) — в первый тур
        if t["round"] not in ids:
            t["round"] = p["rounds"][0]["id"]
    return p


def question_seconds(project, rnd):
    """Время на вопрос: своё у тура или общее."""
    return float(rnd.get("seconds") or project["timing"]["question"])


def group_rounds(project, rows):
    """[(тур, строки тура)] в порядке туров, пустые туры пропускаются."""
    groups = [(r, [row for row in rows if row["round"] == r["id"]]) for r in project["rounds"]]
    return [(r, rs) for r, rs in groups if rs]


# ───────────────────────── мелочи ─────────────────────────

def plural(n, one, few, many):
    n = abs(int(n)) % 100
    if 11 <= n <= 14:
        return many
    n %= 10
    return one if n == 1 else few if 2 <= n <= 4 else many


class _SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def fmt(template, **values):
    try:
        return (template or "").format_map(_SafeDict(values))
    except (ValueError, IndexError):
        return template or ""


def text_values(count, seconds, n=None, total=None, round=""):
    sec = float(seconds)
    sec_txt = f"{sec:g}"
    return {
        "n": n if n is not None else "", "total": total if total is not None else count, "count": count,
        "songs": f"{count} {plural(count, 'песня', 'песни', 'песен')}",
        "seconds": f"{sec_txt} {plural(int(sec), 'секунда', 'секунды', 'секунд') if sec == int(sec) else 'секунды'}",
        "round": round,
    }


def parse_time(value):
    s = str(value if value is not None else "").strip().replace(",", ".")
    if not s:
        return 0.0
    total = 0.0
    for part in s.split(":"):
        total = total * 60 + float(part)
    if total < 0:
        raise ValueError(value)
    return total


def fmt_ts(seconds):
    s = int(seconds or 0)
    h, m, s = s // 3600, s % 3600 // 60, s % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


TRANSLIT = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
                    ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "i", "k", "l", "m", "n", "o", "p", "r",
                     "s", "t", "u", "f", "kh", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"]))


def slugify(name):
    s = "".join(TRANSLIT.get(c, c) for c in (name or "").lower())
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:48] or "quiz"


def safe_filename(name):
    name = Path(name or "file").name
    stem, suffix = os.path.splitext(name)
    stem = re.sub(r"[^\w\-. ]+", "_", stem).strip(" .") or "file"
    return stem[:80] + re.sub(r"[^\w.]", "", suffix.lower())[:8]


# ───────────────────────── рабочая папка и проекты ─────────────────────────

class Workspace:
    def __init__(self, root=ROOT):
        self.root = Path(root).absolute()
        self.projects = self.root / "projects"
        self.library = self.root / "library"
        self._locks = {}
        self._guard = threading.Lock()

    # --- пути ---
    def resolve(self, rel):
        if not rel:
            return None
        p = Path(str(rel)).expanduser()
        return p if p.is_absolute() else self.root / p

    def rel(self, path):
        p = Path(path).absolute()
        try:
            return p.relative_to(self.root).as_posix()
        except ValueError:
            return str(p)

    def project_dir(self, slug):
        if not re.fullmatch(r"[\w\-]+", slug or ""):
            raise QuizError(f"Неправильное имя проекта: {slug}")
        return self.projects / slug

    def lock(self, slug):
        with self._guard:
            return self._locks.setdefault(slug, threading.RLock())

    # --- настройки рабочей папки ---
    def settings(self):
        path = self.library / "settings.json"
        data = {"ytdlp_args": ""}
        if path.is_file():
            try:
                data.update(json.loads(path.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                pass
        return data

    def save_settings(self, data):
        cur = self.settings()
        cur.update({k: v for k, v in data.items() if k in cur})
        self.library.mkdir(parents=True, exist_ok=True)
        (self.library / "settings.json").write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
        return cur

    # --- проекты ---
    def list_projects(self):
        out = []
        if not self.projects.is_dir():
            return out
        for d in self.projects.iterdir():
            f = d / "project.json"
            if f.is_file():
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                out.append({"slug": d.name, "name": data.get("name") or d.name,
                            "tracks": len(data.get("tracks") or []), "updated": f.stat().st_mtime})
        return sorted(out, key=lambda p: -p["updated"])

    def create(self, name, data=None):
        base = slugify(name)
        slug, i = base, 2
        while (self.projects / slug).exists():
            slug, i = f"{base}-{i}", i + 1
        project = normalize(data or {})
        project["name"] = name or project["name"]
        if not (data or {}).get("texts", {}).get("intro_title") and name:
            project["texts"]["intro_title"] = name
        self.project_dir(slug).mkdir(parents=True)
        self.save(slug, project)
        return slug

    def duplicate(self, slug, name):
        data = self.load(slug)
        data["name"] = name
        for t in data["tracks"]:
            t["id"] = ""
        return self.create(name, data)

    def load(self, slug):
        f = self.project_dir(slug) / "project.json"
        if not f.is_file():
            raise QuizError(f"Нет проекта «{slug}»")
        try:
            return normalize(json.loads(f.read_text(encoding="utf-8")))
        except json.JSONDecodeError as e:
            raise QuizError(f"project.json повреждён: {e}")

    def save(self, slug, project):
        d = self.project_dir(slug)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "project.json.tmp"
        tmp.write_text(json.dumps(project, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, d / "project.json")

    def update(self, slug, fn):
        with self.lock(slug):
            project = self.load(slug)
            fn(project)
            self.save(slug, project)
            return project

    def patch(self, slug, set_=None, unset=None):
        """set_ = {"texts.prompt": "…", "visualizer.bars": 96}, unset = ["visualizer.bars"]."""
        def apply(p):
            for path, value in (set_ or {}).items():
                keys = path.split(".")
                if keys[0] not in PATCHABLE:
                    raise QuizError(f"Нельзя менять {path}")
                node, defaults = p, DEFAULT_PROJECT
                for k in keys[:-1]:
                    node = node.setdefault(k, {})
                    defaults = defaults.get(k, {}) if isinstance(defaults, dict) else {}
                default = defaults.get(keys[-1]) if isinstance(defaults, dict) else None
                node[keys[-1]] = _coerce(value, default, path)
            for path in unset or []:
                keys = path.split(".")
                node = p
                for k in keys[:-1]:
                    node = node.get(k, {})
                if isinstance(node, dict):
                    node.pop(keys[-1], None)
            config_from_dict(p["visualizer"], "визуализаторе")  # проверка
        try:
            return self.update(slug, apply)
        except ValueError as e:
            raise QuizError(str(e))

    def patch_track(self, slug, track_id, fields_, internal=False):
        def apply(p):
            t = _find_track(p, track_id)
            for k, v in fields_.items():
                if k not in DEFAULT_TRACK or k == "id" or (not internal and k not in TRACK_EDITABLE):
                    raise QuizError(f"Нельзя менять поле {k}")
                if k == "start2":
                    v = None if v in ("", None) else max(0.0, round(parse_time(v), 2) if isinstance(v, str) else float(v))
                elif k == "start":
                    v = round(parse_time(v), 2) if isinstance(v, str) else max(0.0, float(v or 0))
                elif k == "enabled":
                    v = bool(v)
                elif k != "duration":
                    v = "" if v is None else str(v)
                t[k] = v
        return self.update(slug, apply)

    def fill_track(self, slug, track_id, meta):
        """Дописывает данные, найденные автоматически, не трогая то, что ввёл пользователь."""
        def apply(p):
            try:
                t = _find_track(p, track_id)
            except QuizError:
                return
            for k, v in meta.items():
                if k in ("title", "artist", "album", "year"):
                    if not t.get(k) and v:
                        t[k] = str(v)
                else:
                    t[k] = v
        return self.update(slug, apply)

    def add_tracks(self, slug, tracks):
        return self.update(slug, lambda p: p["tracks"].extend(new_track(**t) for t in tracks))

    def remove_track(self, slug, track_id):
        return self.update(slug, lambda p: p.__setitem__("tracks", [t for t in p["tracks"] if t["id"] != track_id]))

    def reorder(self, slug, ids):
        def apply(p):
            by_id = {t["id"]: t for t in p["tracks"]}
            ordered = [by_id.pop(i) for i in ids if i in by_id]
            p["tracks"] = ordered + list(by_id.values())
        return self.update(slug, apply)

    # --- файлы ---
    def save_upload(self, kind, name, stream, length):
        folders = {"font": "fonts", "image": "images", "cover": "covers/manual", "audio": "audio", "music": "music"}
        allowed = {"font": {".ttf", ".otf"}, "image": {".jpg", ".jpeg", ".png", ".webp"},
                   "cover": {".jpg", ".jpeg", ".png", ".webp"},
                   "audio": {".mp3", ".m4a", ".flac", ".wav", ".ogg", ".opus", ".webm", ".aac"}}
        allowed["music"] = allowed["audio"]
        if kind not in folders:
            raise QuizError(f"Неизвестный тип файла: {kind}")
        name = safe_filename(name)
        if Path(name).suffix.lower() not in allowed[kind]:
            raise QuizError(f"Подходят файлы: {', '.join(sorted(allowed[kind]))}")
        folder = self.library / folders[kind]
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / f".upload-{secrets.token_hex(4)}"
        h = hashlib.sha1()
        with open(tmp, "wb") as f:
            left = length
            while left > 0:
                chunk = stream.read(min(1 << 20, left))
                if not chunk:
                    break
                f.write(chunk)
                h.update(chunk)
                left -= len(chunk)
        dest = folder / name
        if dest.exists():
            if _sha1(dest) == h.hexdigest():
                tmp.unlink()
                return self.rel(dest)
            stem, suffix = os.path.splitext(name)
            dest = folder / f"{stem}-{h.hexdigest()[:6]}{suffix}"
        os.replace(tmp, dest)
        if kind == "font":
            try:
                ImageFont.truetype(str(dest), 20)
            except OSError:
                dest.unlink()
                raise QuizError("Этот файл шрифта не открывается")
        return self.rel(dest)

    def link_local(self, path):
        """Свой файл с диска: ссылка на него в library/local, без копирования."""
        src = Path(path).expanduser().absolute()
        folder = self.library / "local"
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / f"{safe_filename(src.stem)}-{hashlib.sha1(str(src).encode()).hexdigest()[:6]}{src.suffix.lower()}"
        if not dest.exists():
            dest.symlink_to(src)
        return self.rel(dest)

    def library_files(self, kind):
        folder = {"font": "fonts", "image": "images", "cover": "covers/manual", "music": "music"}[kind]
        d = self.library / folder
        if not d.is_dir():
            return []
        return [self.rel(p) for p in sorted(d.iterdir()) if p.is_file() and not p.name.startswith(".")]


def local_meta(path):
    """Название, исполнитель, альбом, год и длительность из тегов файла (или из имени «Исполнитель - Песня»)."""
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:format_tags:stream_tags",
                        "-of", "json", str(path)], capture_output=True, text=True)
    try:
        data = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        data = {}
    tags = {}
    for src in [data.get("format", {})] + data.get("streams", []):
        for k, v in (src.get("tags") or {}).items():
            tags.setdefault(k.lower(), str(v).strip())
    title, artist = tags.get("title", ""), tags.get("artist") or tags.get("album_artist", "")
    if not title:
        stem = Path(path).stem
        parts = re.split(r"\s+[-–—]\s+", stem, maxsplit=1)
        artist, title = (parts if len(parts) == 2 else (artist, stem))
    year = re.search(r"\d{4}", tags.get("date") or tags.get("year") or "")
    try:
        duration = float(data.get("format", {}).get("duration"))
    except (TypeError, ValueError):
        duration = None
    return {"title": title, "artist": artist, "album": tags.get("album", ""),
            "year": year.group(0) if year else "", "duration": duration}


def _sha1(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _find_track(project, track_id):
    for t in project["tracks"]:
        if t["id"] == track_id:
            return t
    raise QuizError("Трек не найден — возможно, его уже удалили")


def _coerce(value, default, path):
    try:
        if isinstance(default, bool):
            return bool(value)
        if isinstance(default, (int, float)) and not isinstance(default, bool):
            v = float(value)
            if path.startswith("timing.") and v < 0:
                raise ValueError
            return v
        if isinstance(default, str):
            return "" if value is None else str(value)
    except (TypeError, ValueError):
        raise QuizError(f"{path}: неправильное значение {value!r}")
    return value


# ───────────────────────── импорт списка треков ─────────────────────────

ALIASES = {
    "url": "url", "ссылка": "url", "link": "url", "youtube": "url",
    "file": "file", "файл": "file", "трек": "file",
    "start": "start", "начало": "start", "старт": "start",
    "title": "title", "название": "title", "песня": "title",
    "artist": "artist", "исполнитель": "artist", "артист": "artist",
    "hint": "hint", "подсказка": "hint",
    "cover": "cover", "обложка": "cover",
    "album": "album", "альбом": "album",
    "year": "year", "год": "year",
}


def parse_track_list(text, ws, base_dir=None):
    """Разбирает вставленный список. Понимает:
        ссылка
        ссылка 1:05
        ссылка | 1:05 | Название | Исполнитель | подсказка
        /путь/к/файлу.mp3 | 0:48
        CSV/таблицу с заголовком (url или file, start, title, artist, …)
    Возвращает (треки, ошибки)."""
    from youtube import is_url, parse_url

    base_dir = Path(base_dir) if base_dir else None
    lines = [l for l in text.splitlines() if l.strip() and not l.strip().startswith("#")]
    if not lines:
        return [], []
    first = lines[0]
    delim = max(",;\t|", key=first.count)
    header = [h.strip().lower() for h in first.split(delim)]
    rows = []
    if first.count(delim) and sum(h in ALIASES for h in header) >= 2:
        reader = csv.reader(io.StringIO("\n".join(lines[1:])), delimiter=delim)
        keys = [ALIASES.get(h) for h in header]
        for i, cells in enumerate(reader, start=2):
            rows.append((i, {k: c.strip() for k, c in zip(keys, cells) if k}))
    else:
        for i, line in enumerate(lines, start=1):
            parts = [p.strip() for p in re.split(r"\s*[|\t;]\s*", line.strip())]
            if len(parts) == 1:
                tokens = line.strip().rsplit(maxsplit=1)
                if len(tokens) == 2 and re.fullmatch(r"[\d:.,]+", tokens[1]):
                    parts = tokens
            names = ["source", "start", "title", "artist", "hint"]
            rows.append((i, dict(zip(names, parts))))

    tracks, errors = [], []
    for line_no, row in rows:
        source = row.get("source") or row.get("url") or row.get("file") or ""
        t = {k: row.get(k, "") for k in ("title", "artist", "hint", "album", "year")}
        try:
            start = parse_time(row["start"]) if row.get("start") else None
        except ValueError:
            errors.append(f"строка {line_no}: не понимаю время «{row['start']}»")
            continue
        if is_url(source):
            _, url_start, _ = parse_url(source)
            t["url"] = source
            start = start if start is not None else url_start
        else:
            p = Path(source).expanduser()
            if not p.is_absolute() and base_dir:
                p = base_dir / p
            if not p.is_file():
                errors.append(f"строка {line_no}: это не ссылка и не файл — «{source[:80]}»")
                continue
            t["file"] = ws.rel(p) if str(p.absolute()).startswith(str(ws.root)) else ws.link_local(p)
            meta = local_meta(p)
            for k in ("title", "artist", "album", "year"):
                t[k] = t[k] or meta[k]
            t["duration"] = meta["duration"]
        if row.get("cover"):
            c = Path(row["cover"]).expanduser()
            if not c.is_absolute() and base_dir:
                c = base_dir / c
            if c.is_file():
                t["cover"] = ws.rel(c)
            else:
                errors.append(f"строка {line_no}: нет картинки {row['cover']}")
        t["start"] = start or 0.0
        tracks.append(t)
    return tracks, errors


# ───────────────────────── шрифты ─────────────────────────

SYSTEM_FONTS = [
    ("/usr/share/fonts/truetype/ubuntu/Ubuntu[wdth,wght].ttf", "Bold"),
    ("/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf", ""),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", ""),
    ("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf", ""),
    ("/usr/share/fonts/truetype/freefont/FreeSansBold.ttf", ""),
]


def split_font_spec(spec):
    path, _, style = (spec or "").partition("#")
    return path, style


@lru_cache(maxsize=256)
def load_font(path, style, size):
    font = ImageFont.truetype(path, max(8, int(size)))
    if style:
        try:
            font.set_variation_by_name(style)
        except Exception:
            pass
    return font


def font_styles(path):
    """Начертания вариативного шрифта (Regular, Bold, …) или [] для обычного."""
    try:
        names = ImageFont.truetype(str(path), 20).get_variation_names()
        return [n.decode() if isinstance(n, bytes) else str(n) for n in names]
    except Exception:
        return []


def default_font():
    for path, style in SYSTEM_FONTS:
        if os.path.isfile(path):
            if style and style.encode() not in [n if isinstance(n, bytes) else n.encode()
                                               for n in _safe_variations(path)]:
                style = ""
            return path, style
    raise QuizError("Не нашёл ни одного шрифта. Загрузите свой .ttf в конструкторе.")


def _safe_variations(path):
    try:
        return ImageFont.truetype(path, 20).get_variation_names()
    except Exception:
        return []


class Fonts:
    """Два шрифта: heading — заголовки, название песни, цифры таймера; text — всё остальное."""

    def __init__(self, ws, specs):
        self.roles = {}
        for role in ("heading", "text"):
            path, style = split_font_spec((specs or {}).get(role, ""))
            full = ws.resolve(path) if path else None
            if full and full.is_file():
                self.roles[role] = (str(full), style)
            else:
                self.roles[role] = None
        fallback = self.roles["heading"] or self.roles["text"] or default_font()
        for role in self.roles:
            self.roles[role] = self.roles[role] or fallback

    def get(self, role, size):
        path, style = self.roles[role]
        return load_font(path, style, int(size))


# ───────────────────────── текст на картинке ─────────────────────────

def line_height(font):
    ascent, descent = font.getmetrics()
    return int((ascent + descent) * 1.1)


def wrap_text(draw, text, font, max_w):
    lines = []
    for paragraph in text.split("\n"):  # переводы строк, введённые пользователем, сохраняются
        cur = ""
        for word in paragraph.split():
            candidate = f"{cur} {word}".strip()
            if not cur or draw.textlength(candidate, font=font) <= max_w:
                cur = candidate
            else:
                lines.append(cur)
                cur = word
        lines.append(cur)
    return lines or [""]


def fit_text(draw, text, max_w, size, min_size, max_lines, font_fn):
    """Подбирает размер шрифта и переносы, чтобы текст влез по ширине."""
    size, min_size = int(size), int(min_size)
    while True:
        font = font_fn(size)
        lines = wrap_text(draw, text, font, max_w)
        fits = len(lines) <= max_lines and all(draw.textlength(l, font=font) <= max_w for l in lines)
        if fits or size <= min_size:
            return font, lines
        size = max(min_size, int(size * 0.9))


def draw_lines(draw, lines, font, x, y, fill, anchor="ma"):
    lh = line_height(font)
    for i, line in enumerate(lines):
        draw.text((x, y + i * lh), line, font=font, fill=fill, anchor=anchor)
    return y + len(lines) * lh


# ───────────────────────── экраны ─────────────────────────

class Renderer:
    def __init__(self, width, height, style, fonts, ring_cfg=None, background=None):
        self.W, self.H = width, height
        self.u = min(width, height)
        self.cx = width // 2
        self.cy = int(height * (0.45 if width >= height else 0.40))
        self.style = style
        self.fonts = fonts
        self.bg = self._background(background)
        self.ring = (SpectrumRing(ring_cfg, self.u, lambda s: fonts.get("heading", s))
                     if ring_cfg is not None else None)

    def c(self, name):
        return ImageColor.getrgb(self.style[name])

    def hf(self, size):
        return self.fonts.get("heading", size)

    def tf(self, size):
        return self.fonts.get("text", size)

    def _background(self, path):
        W, H = self.W, self.H
        if path and Path(path).is_file():
            img = ImageOps.fit(Image.open(path).convert("RGB"), (W, H), Image.Resampling.LANCZOS)
            return Image.blend(img, Image.new("RGB", (W, H), (0, 0, 0)), 0.5)
        grad = Image.linear_gradient("L").resize((W, H))
        bg = Image.composite(Image.new("RGB", (W, H), self.c("bg_bottom")),
                             Image.new("RGB", (W, H), self.c("bg_top")), grad)
        glow = ImageOps.invert(Image.radial_gradient("L")).resize((W, H)).point(lambda v: v * 45 // 100)
        return Image.composite(Image.new("RGB", (W, H), self.c("glow")), bg, glow)

    def _header(self, img, text):
        if not text:
            return self.H * 0.04
        font = self.tf(self.u * 0.045)
        d = ImageDraw.Draw(img)
        cy = self.H * 0.075
        w = d.textlength(text, font=font)
        h = sum(font.getmetrics())
        box = (self.cx - w / 2 - h * 0.7, cy - h / 2 - h * 0.35,
               self.cx + w / 2 + h * 0.7, cy + h / 2 + h * 0.35)
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(overlay).rounded_rectangle(box, radius=(box[3] - box[1]) / 2, fill=(255, 255, 255, 38))
        img.paste(overlay, (0, 0), overlay)
        d.text((self.cx, cy), text, font=font, fill=self.c("muted"), anchor="mm")
        return box[3]

    def _equalizer(self, d, bottom, color):
        heights = [0.35, 0.7, 1.0, 0.55, 0.85, 0.45, 0.65]
        bw, gap, max_h = self.u * 0.04, self.u * 0.022, self.u * 0.14
        x = self.cx - (len(heights) * bw + (len(heights) - 1) * gap) / 2
        for h in heights:
            d.rounded_rectangle((x, bottom - max_h * h, x + bw, bottom), radius=bw / 2, fill=color)
            x += bw + gap

    def title_card(self, title, subtitle="", sub_max_lines=2):
        img = self.bg.copy()
        d = ImageDraw.Draw(img)
        max_w = self.W * 0.86
        font, lines = fit_text(d, title or " ", max_w, self.u * 0.13, self.u * 0.06, 3, self.hf)
        sub_font, sub_lines = (fit_text(d, subtitle, max_w, self.u * 0.055, self.u * 0.03, sub_max_lines, self.tf)
                               if subtitle else (None, []))
        eq_h, gap = self.u * 0.14, self.u * 0.06
        total = eq_h + gap + line_height(font) * len(lines)
        if sub_lines:
            total += gap * 0.4 + line_height(sub_font) * len(sub_lines)
        y = (self.H - total) / 2
        self._equalizer(d, y + eq_h, self.c("accent"))
        y = draw_lines(d, lines, font, self.cx, y + eq_h + gap, self.c("text"))
        if sub_lines:
            draw_lines(d, sub_lines, sub_font, self.cx, y + gap * 0.4, self.c("muted"))
        return img

    # --- вопрос ---

    def question_base(self, label, prompt, hint=""):
        img = self.bg.copy()
        self._header(img, label)
        self.ring.paint_static(img, (self.cx, self.cy))
        d = ImageDraw.Draw(img)
        max_w = self.W * 0.86
        y = self.cy + self.ring.visual_radius + self.u * 0.045
        if prompt:
            font, lines = fit_text(d, prompt, max_w, self.u * 0.07, self.u * 0.045, 2, self.hf)
            y = draw_lines(d, lines, font, self.cx, y, self.c("text"))
        if hint:
            font, lines = fit_text(d, hint, max_w, self.u * 0.045, self.u * 0.032, 2, self.tf)
            draw_lines(d, lines, font, self.cx, y + self.u * 0.01, self.c("muted"))
        return img

    def question_frame(self, base, levels, i, n_frames, frame=None, clean=None, pre_frames=0):
        """n_frames — кадры отсчёта; pre_frames — пауза перед ним (кольцо полное, звука нет)."""
        box = self.ring.box((self.cx, self.cy))
        if frame is None:
            frame = base.copy()
        else:
            frame.paste(clean if clean is not None else base.crop(box), box[:2])
        self.ring.draw(frame, (self.cx, self.cy), levels[i], i / FPS, n_frames / FPS, pre_frames / FPS)
        return frame

    def question_frames(self, base, levels, n_frames, pre_frames=0):
        """Кадры вопроса: визуализатор по настоящему спектру + обратный отсчёт."""
        frame = base.copy()
        clean = base.crop(self.ring.box((self.cx, self.cy)))
        for i in range(pre_frames + n_frames):
            yield self.question_frame(base, levels, i, n_frames, frame, clean, pre_frames)

    # --- ответ ---

    def _backdrop(self, cover):
        small = ImageOps.fit(cover, (max(16, self.W // 10), max(16, self.H // 10)), Image.Resampling.BILINEAR)
        big = small.filter(ImageFilter.GaussianBlur(3)).resize((self.W, self.H), Image.Resampling.BICUBIC)
        return Image.blend(big, Image.new("RGB", (self.W, self.H), (0, 0, 0)), 0.6)

    def _vinyl(self, size):
        """Заглушка, если обложку найти не удалось."""
        ss = 3
        S = size * ss
        img = Image.new("RGB", (S, S), (38, 26, 64))
        d = ImageDraw.Draw(img)
        c, r = S / 2, S * 0.44

        def circle(rr, **kw):
            d.ellipse((c - rr, c - rr, c + rr, c + rr), **kw)

        circle(r, fill=(16, 12, 22))
        for k in range(10):
            circle(r * (0.45 + 0.52 * k / 10), outline=(44, 38, 58), width=ss)
        circle(r * 0.36, fill=self.c("accent"))
        circle(r * 0.05, fill=(38, 26, 64))
        return img.resize((size, size), Image.Resampling.LANCZOS)

    def _paste_cover(self, img, cover, x, y, size):
        art = (ImageOps.fit(cover, (size, size), Image.Resampling.LANCZOS) if cover is not None
               else self._vinyl(size))
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=int(size * 0.035), fill=255)
        pad = int(self.u * 0.04)
        shadow = Image.new("L", (size + pad * 2, size + pad * 2), 0)
        shadow.paste(mask.point(lambda v: v * 70 // 100), (pad, pad))
        shadow = shadow.filter(ImageFilter.GaussianBlur(pad / 2))
        img.paste((0, 0, 0), (x - pad, y - pad + pad // 3), shadow)
        img.paste(art, (x, y), mask)

    def _answer_texts(self, d, row, max_w):
        # крупно — ответ (по умолчанию исполнитель), под ним песня
        answer = row.get("answer") or row.get("artist") or row["title"]
        artist_line = (row.get("artist") or "").replace("\n", ", ")
        song = " — ".join(p for p in (artist_line, row["title"]) if p) if row.get("answer") else row["title"]
        blocks = []
        font, lines = fit_text(d, answer or " ", max_w, self.u * 0.08, self.u * 0.045, 3, self.hf)
        blocks.append((font, lines, self.c("text"), 0))
        if song and song != answer:
            font, lines = fit_text(d, song, max_w, self.u * 0.055, self.u * 0.035, 2, self.tf)
            blocks.append((font, lines, self.c("accent"), self.u * 0.015))
        release = " · ".join(p for p in (row.get("album", ""), str(row.get("year", ""))) if p)
        if release:
            font, lines = fit_text(d, release, max_w, self.u * 0.04, self.u * 0.03, 2, self.tf)
            blocks.append((font, lines, self.c("muted"), self.u * 0.025))
        return blocks

    def answer_card(self, label, row):
        cover = None
        if row.get("cover_file"):
            try:
                cover = Image.open(row["cover_file"]).convert("RGB")
            except Exception:
                cover = None
        img = self._backdrop(cover) if cover is not None and self.style.get("cover_backdrop") else self.bg.copy()
        header_bottom = self._header(img, label)
        d = ImageDraw.Draw(img)
        gap = int(self.u * 0.06)
        horizontal = self.W >= self.H

        if horizontal:
            size = int(self.H * 0.5)
            blocks = self._answer_texts(d, row, self.W * 0.88 - size - gap)
        else:
            size = int(self.W * 0.62)
            blocks = self._answer_texts(d, row, self.W * 0.86)
        text_h = sum(line_height(f) * len(ls) + sp for f, ls, _, sp in blocks)
        text_w = max(d.textlength(l, font=f) for f, ls, _, _ in blocks for l in ls)

        if horizontal:
            x = int((self.W - (size + gap + text_w)) / 2)
            yc = (header_bottom + self.H) / 2
            self._paste_cover(img, cover, x, int(yc - size / 2), size)
            tx, ty, anchor = x + size + gap, yc - text_h / 2, "la"
        else:
            y = header_bottom + (self.H - header_bottom - (size + gap + text_h)) / 2
            self._paste_cover(img, cover, self.cx - size // 2, int(y), size)
            tx, ty, anchor = self.cx, y + size + gap, "ma"
        for font, lines, color, spacing in blocks:
            ty = draw_lines(d, lines, font, tx, ty + spacing, color, anchor)
        return img


def ring_config(project):
    try:
        return config_from_dict(project.get("visualizer") or {}, "визуализаторе")
    except ValueError as e:
        raise QuizError(str(e))


def make_renderer(ws, project, size=None):
    W, H = size or ((1080, 1920) if project["format"] == "vertical" else (1920, 1080))
    style = project["style"]
    for key in ("bg_top", "bg_bottom", "glow", "text", "muted", "accent"):
        try:
            ImageColor.getrgb(style[key])
        except (ValueError, KeyError):
            raise QuizError(f"Не понимаю цвет {key}: «{style.get(key)}»")
    return Renderer(W, H, style, Fonts(ws, project["fonts"]), ring_config(project),
                    ws.resolve(style.get("background")))


# ───────────────────────── звук ─────────────────────────

def probe_duration(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return False, None
    try:
        return True, float(r.stdout.strip())
    except ValueError:
        return True, None


def extract_fragment(src, start, n_samples):
    """Вырезает отрывок и выравнивает громкость. Возвращает numpy (n_samples, 2), -1..1."""
    dur = n_samples / SAMPLE_RATE
    af = f"loudnorm=I={LOUDNESS}:TP=-1.5:LRA=11,aresample={SAMPLE_RATE},apad,atrim=end_sample={n_samples}"
    cmd = ["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{dur + 1:.3f}", "-i", str(src),
           "-vn", "-map_metadata", "-1", "-af", af, "-ac", "2", "-ar", str(SAMPLE_RATE),
           "-f", "s16le", "-"]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        raise QuizError(f"Не удалось вырезать отрывок из {Path(src).name}:\n{r.stderr.decode(errors='replace').strip()}")
    data = np.frombuffer(r.stdout, dtype="<i2")
    data = data[:len(data) // 2 * 2].reshape(-1, 2)
    audio = np.zeros((n_samples, 2), dtype=np.float32)
    audio[:min(n_samples, len(data))] = data[:n_samples] / 32768.0
    return audio


_fragment_cache = OrderedDict()
_fragment_lock = threading.Lock()


def speech(ws, voice, text):
    """Озвучка текста: голоса Microsoft через edge-tts (ru-RU-…) или Piper (ru_RU-…).
    Готовая речь кэшируется, громкость выравнивается так же, как у отрывков."""
    folder = ws.library / "voices"
    edge = voice.startswith("ru-RU-")
    out = folder / "speech" / f"{hashlib.sha1((voice + chr(10) + text).encode()).hexdigest()}.{'mp3' if edge else 'wav'}"
    if not out.is_file():
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".part" + out.suffix)
        if edge:
            import edge_tts
            for attempt in range(12):  # сервис Microsoft отвечает через раз — повторяем
                try:
                    edge_tts.Communicate(text, voice).save_sync(str(tmp))
                    break
                except Exception as e:
                    error = e
                    time.sleep(1 + attempt)
            else:
                raise QuizError(f"Сервис Microsoft не озвучил текст «{text[:40]}» за 12 попыток "
                                f"(нужен интернет, или попробуйте другой голос): {error}")
        else:
            model = folder / f"{voice}.onnx"
            if not model.is_file():  # голос скачивается один раз, ~60 МБ
                subprocess.run([sys.executable, "-m", "piper.download_voices", "--data-dir", str(folder), voice],
                               check=True, capture_output=True)
            from piper import PiperVoice
            with wave.open(str(tmp), "wb") as w:
                PiperVoice.load(str(model)).synthesize_wav(text, w)
        os.replace(tmp, out)
    _, seconds = probe_duration(out)
    return extract_fragment(out, 0, round(seconds * SAMPLE_RATE))


def extract_fragment_cached(src, start, n_samples):
    """То же, но с памятью на несколько последних отрывков — для быстрого предпросмотра."""
    try:
        mtime = os.stat(src).st_mtime
    except OSError:
        mtime = 0
    key = (str(src), mtime, round(start, 3), n_samples)
    with _fragment_lock:
        if key in _fragment_cache:
            _fragment_cache.move_to_end(key)
            return _fragment_cache[key]
    audio = extract_fragment(src, start, n_samples)
    with _fragment_lock:
        _fragment_cache[key] = audio
        while len(_fragment_cache) > 8:
            _fragment_cache.popitem(last=False)
    return audio


def with_fades(audio, fade_in, fade_out):
    x = audio.copy()
    n_in, n_out = int(fade_in * SAMPLE_RATE), int(fade_out * SAMPLE_RATE)
    if n_in:
        x[:n_in] *= np.linspace(0, 1, n_in, dtype=np.float32)[:, None]
    if n_out:
        x[-n_out:] *= np.linspace(1, 0, n_out, dtype=np.float32)[:, None]
    return x


def waveform(ws, path, buckets=1000):
    """Огибающая трека для отрисовки в конструкторе (кэшируется)."""
    path = Path(path)
    st = path.stat()
    key = hashlib.sha1(f"{path.absolute()}|{st.st_size}|{int(st.st_mtime)}|{buckets}".encode()).hexdigest()[:20]
    cache = ws.library / ".cache" / "waveforms" / f"{key}.json"
    if cache.is_file():
        return json.loads(cache.read_text())
    rate = 4000
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(rate),
                        "-f", "s16le", "-"], capture_output=True)
    x = np.abs(np.frombuffer(r.stdout, dtype="<i2").astype(np.float32))
    duration = len(x) / rate
    if len(x) < buckets:
        peaks = np.zeros(buckets)
    else:
        x = x[:len(x) // buckets * buckets].reshape(buckets, -1)
        peaks = np.sqrt(np.mean(x ** 2, axis=1))  # RMS — видно припевы, а не только пики
        top = np.percentile(peaks, 99) or 1
        peaks = np.clip(peaks / top, 0, 1)
    data = {"duration": duration, "peaks": [round(float(v), 3) for v in peaks]}
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(data))
    return data


# ───────────────────────── подготовка к сборке ─────────────────────────

def track_label(t):
    label = f"{t['artist']} — {t['title']}" if t.get("artist") else (t.get("title") or t.get("url") or "без названия")
    return label.replace("\n", ", ")  # переносы — только для слайдов


def prepare_rows(ws, project, need_audio=True):
    """Включённые треки с полными путями. Проверяет, что всё скачано и время начала в пределах трека."""
    rows, errors = [], []
    for i, t in enumerate(project["tracks"], start=1):
        if not t.get("enabled", True):
            continue
        path = ws.resolve(t.get("file"))
        if need_audio and (not path or not path.is_file()):
            errors.append(f"№{i} {track_label(t)}: трек ещё не скачан")
            continue
        duration = t.get("duration")
        if need_audio and duration is None:
            _, duration = probe_duration(path)
        if duration and t["start"] >= duration:
            errors.append(f"№{i} {track_label(t)}: начало {fmt_ts(t['start'])} позже конца трека ({fmt_ts(duration)})")
            continue
        rows.append({
            "id": t["id"], "round": t["round"], "path": path, "start": float(t["start"] or 0), "start2": t["start2"],
            "title": t["title"] or (path.stem if path else "без названия"), "artist": t["artist"],
            "album": t["album"], "year": t["year"], "hint": t["hint"],
            "question": t["question"], "answer": t["answer"],
            "cover": ws.resolve(t["cover"]) if t.get("cover") else None,
            "thumb": ws.resolve(t["thumb"]) if t.get("thumb") else None,
        })
    if errors:
        raise QuizError("Исправьте треки:\n" + "\n".join(f"• {e}" for e in errors))
    return rows


def cover_finder(ws, project):
    return CoverFinder(ws.library / "covers" / "auto", online=project["covers"]["online"],
                       itunes_country=project["covers"].get("itunes_country") or "US")


def apply_covers(rows, results):
    for row, info in zip(rows, results):
        row.update(cover_file=info["file"], cover_source=info["source"])
        row["album"] = row["album"] or info["album"]
        row["year"] = row["year"] or info["year"]


def find_covers(ws, slug, track_ids=None, log=print, progress=None, cancel=None, forget=False):
    """Ищет обложки и записывает результат в проект (cover_auto, cover_source, альбом, год)."""
    project = ws.load(slug)
    rows = [r for r in prepare_rows_loose(ws, project) if not track_ids or r["id"] in track_ids]
    finder = cover_finder(ws, project)
    if forget:
        for r in rows:
            finder.forget(r["artist"], r["title"])
    done = [0]

    def on_done(i, info):
        if cancel is not None and cancel.is_set():
            return
        r = rows[i]
        ws.fill_track(slug, r["id"], {
            "cover_auto": ws.rel(info["file"]) if info["file"] else "",
            "cover_source": info["source"], "album": info["album"], "year": info["year"]})
        done[0] += 1
        log(f"{track_label(r)}: {info['source']}")
        if progress:
            progress(done[0] / max(1, len(rows)), "Ищу обложки")

    finder.find_all(rows, on_done)
    if finder.offline_reason:
        log(f"Поиск в интернете не удался: {finder.offline_reason}")
    return {"tracks": len(rows), "offline": finder.offline_reason}


def download_tracks(ws, slug, ids=None, log=print, states=None, progress=None, cancel=None, workers=3):
    """Скачивает треки по ссылкам (yt-dlp), дописывает в проект название, исполнителя, длительность,
    затем ищет обложки. states — словарь, куда пишется состояние каждого трека (для интерфейса)."""
    import shlex
    import youtube

    project = ws.load(slug)
    todo = [t for t in project["tracks"] if t.get("url") and (not ids or t["id"] in ids)
            and not (ws.resolve(t.get("file")) and ws.resolve(t["file"]).is_file())]
    if not todo:
        log("Все треки уже скачаны")
        return {"downloaded": 0, "failed": 0}
    if not youtube.ytdlp_command():
        raise QuizError(f"yt-dlp не найден: {youtube.INSTALL_HINT}")
    tools = youtube.tool_info()
    if tools.get("problem"):
        log(f"Внимание: {tools['problem']}")
    try:
        extra = shlex.split(ws.settings().get("ytdlp_args") or "")
    except ValueError:
        raise QuizError("Не понимаю дополнительные параметры yt-dlp: проверьте кавычки")
    states = states if states is not None else {}
    for t in todo:
        states[t["id"]] = {"state": "queued", "progress": 0.0}
    done, failed = [], []
    progress = progress or (lambda frac, stage: None)

    def update_total():
        progress(sum(s["progress"] for s in states.values()) / len(states) * 0.9,
                 f"Скачано {len(done)} из {len(todo)}")

    def one(t):
        st = states[t["id"]]
        if cancel is not None and cancel.is_set():
            st["state"] = "cancelled"
            return
        st["state"] = "downloading"

        def on_progress(p):
            st["progress"] = p
            update_total()

        try:
            res = youtube.download(t["url"], ws.library / "youtube", on_progress, cancel, extra,
                                   has_js=bool(tools.get("js_runtime")))
            duration = res["duration"] or probe_duration(res["audio"])[1]
            ws.fill_track(slug, t["id"], {
                "file": ws.rel(res["audio"]), "thumb": ws.rel(res["thumb"]) if res["thumb"] else "",
                "duration": duration, "error": "",
                "title": res["title"], "artist": res["artist"], "album": res["album"], "year": res["year"]})
            st.update(state="done", progress=1.0)
            done.append(t["id"])
            shown = {"title": t.get("title") or res["title"], "artist": t.get("artist") or res["artist"]}
            log(f"Скачан: {track_label(shown)}")
        except youtube.Cancelled:
            st["state"] = "cancelled"
        except youtube.DownloadError as e:
            st.update(state="error", error=str(e), progress=1.0)
            ws.fill_track(slug, t["id"], {"error": str(e)})
            failed.append(t["id"])
            log(f"Ошибка: {t['url']} — {e}")
        update_total()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, todo))
    if cancel is not None and cancel.is_set():
        raise Cancelled()
    if done:
        progress(0.92, "Ищу обложки")
        find_covers(ws, slug, set(done), log=log)
    return {"downloaded": len(done), "failed": len(failed)}


def prepare_rows_loose(ws, project):
    """Все треки (даже не скачанные) — для поиска обложек."""
    rows = []
    for t in project["tracks"]:
        rows.append({
            "id": t["id"], "round": t["round"], "path": ws.resolve(t.get("file")), "title": t["title"],
            "artist": t["artist"],
            "album": t["album"], "year": t["year"], "question": t["question"], "answer": t["answer"],
            "cover": ws.resolve(t["cover"]) if t.get("cover") else None,
            "thumb": ws.resolve(t["thumb"]) if t.get("thumb") else None,
        })
    return rows


# ───────────────────────── предпросмотр ─────────────────────────

def _fake_levels(n_frames, n_bars, seed=3):
    rng = np.random.default_rng(seed)
    base = np.convolve(rng.random(n_bars + 8), np.ones(5) / 5, mode="same")[:n_bars]
    pulse = 0.55 + 0.45 * np.abs(np.sin(np.arange(n_frames) / FPS * math.pi * 2))
    return np.clip(base[None, :] * pulse[:, None] * 1.3, 0, 1)


def preview(ws, slug, track_id=None, log=print):
    """Кадры всех экранов в projects/<имя>/output/preview/. Возвращает {экран: путь}."""
    project = ws.load(slug)
    r = make_renderer(ws, project)
    timing, texts = project["timing"], project["texts"]
    enabled = [t for t in project["tracks"] if t.get("enabled", True)]
    count = len(enabled) or 1
    vals = text_values(count, timing["question"])

    track =next((t for t in enabled if t["id"] == track_id), None) or \
        next((t for t in enabled if ws.resolve(t.get("file")) and ws.resolve(t["file"]).is_file()), None) or \
        (enabled[0] if enabled else None)
    rnd = next((r for r in project["rounds"] if track and r["id"] == track["round"]), project["rounds"][0])
    in_round = [t for t in enabled if t["round"] == rnd["id"]]
    q_frames = frames_for(question_seconds(project, rnd))
    index = in_round.index(track) + 1 if track else 1
    row = {"title": "Название песни", "artist": "Исполнитель", "album": "Альбом", "year": "2004", "hint": ""}
    levels = None
    if track:
        row = prepare_rows_loose(ws, {"tracks": [track]})[0]
        row["hint"] = track["hint"]
        row["title"] = row["title"] or "Название песни"
        row["artist"] = row["artist"] or "Исполнитель"
        path = row["path"]
        if path and path.is_file():
            audio = extract_fragment_cached(path, float(track["start"] or 0), q_frames * SAMPLES_PER_FRAME)
            audio = with_fades(audio, 0.1, 0.6)
            levels = analyze(audio.mean(axis=1), SAMPLE_RATE, q_frames, FPS, r.ring.cfg)
        info = cover_finder(ws, project).find(row)
        apply_covers([row], [info])
    if levels is None:
        levels = _fake_levels(q_frames, r.ring.n_bars)

    out_dir = ws.project_dir(slug) / "output" / "preview"
    out_dir.mkdir(parents=True, exist_ok=True)
    round_vals = text_values(len(in_round) or 1, question_seconds(project, rnd), round=rnd["title"])
    label_vals = dict(round_vals, n=index)
    base = r.question_base(fmt(texts["question_label"], **label_vals),
                           fmt(row.get("question") or texts["prompt"], **label_vals), row.get("hint", ""))
    pre = round(timing["gap"] * FPS)  # как в ролике: пауза с полным кольцом перед отсчётом
    levels = np.concatenate([np.zeros((pre, levels.shape[1]), dtype=levels.dtype), levels])
    reveal_s = min(r.ring.cfg.reveal_seconds, pre / FPS) if pre else r.ring.cfg.reveal_seconds
    reveal_i = min(pre + q_frames - 1, int(reveal_s * FPS * 0.45))
    images = {
        "intro": r.title_card(fmt(texts["intro_title"], **vals), fmt(texts["intro_subtitle"], **vals), 8),
        "round": r.title_card(rnd["title"], fmt(rnd["rules"], **round_vals), 8),
        "reveal": r.question_frame(base, levels, reveal_i, q_frames, pre_frames=pre),
        "question": r.question_frame(base, levels, pre + int(q_frames * 0.55), q_frames, pre_frames=pre),
        "answers": r.title_card(fmt(texts["answers_title"], **round_vals), fmt(texts["answers_subtitle"], **round_vals)),
        "answer": r.answer_card(fmt(texts["answer_label"], **label_vals), row),
        "outro": r.title_card(fmt(texts["outro_title"], **vals), fmt(texts["outro_subtitle"], **vals)),
    }
    paths = {}
    for name, img in images.items():
        p = out_dir / f"{name}.jpg"
        img.save(p, quality=90)
        paths[name] = ws.rel(p)
    log(f"Предпросмотр готов (трек: {track_label(track) if track else 'пример'})")
    return {"images": paths, "track_id": track["id"] if track else None}


def answers_sheet(ws, slug, log=print):
    """Все ответы на одном листе — проверить обложки."""
    project = ws.load(slug)
    r = make_renderer(ws, project)
    rows = prepare_rows_loose(ws, {"tracks": [t for t in project["tracks"] if t.get("enabled", True)]})
    if not rows:
        raise QuizError("В квизе нет треков")
    apply_covers(rows, cover_finder(ws, project).find_all(rows))
    n = len(rows)
    cols = 3 if n > 4 else 2 if n > 1 else 1
    tw, th = r.W // 3, r.H // 3
    sheet = Image.new("RGB", (cols * tw, math.ceil(n / cols) * th))
    vals = text_values(n, project["timing"]["question"])
    for i, row in enumerate(rows):
        card = r.answer_card(fmt(project["texts"]["answer_label"], **dict(vals, n=i + 1, total=n)), row)
        sheet.paste(card.resize((tw, th), Image.Resampling.LANCZOS), ((i % cols) * tw, (i // cols) * th))
    path = ws.project_dir(slug) / "output" / f"{slug}_answers.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(path)
    log(f"Сохранил {ws.rel(path)}")
    return ws.rel(path)


# ───────────────────────── сборка ─────────────────────────

class VideoWriter:
    def __init__(self, proc, cancel=None, on_frame=None):
        self.proc = proc
        self.cancel = cancel
        self.on_frame = on_frame
        self.last = None

    def _put(self, data):
        if self.cancel is not None and self.cancel.is_set():
            raise Cancelled()
        try:
            self.proc.stdin.write(data)
        except BrokenPipeError:
            raise QuizError("ffmpeg неожиданно остановился")
        if self.on_frame:
            self.on_frame()

    def segment(self, frames, n):
        """frames — одна картинка (статичный экран) или генератор кадров."""
        static = isinstance(frames, Image.Image)
        it = None if static else iter(frames)
        static_bytes = None
        for i in range(n):
            img = frames if static else next(it)
            if self.last is not None and i < FADE_FRAMES:
                self._put(Image.blend(self.last, img, (i + 1) / (FADE_FRAMES + 1)).tobytes())
            elif static:
                static_bytes = static_bytes or img.tobytes()
                self._put(static_bytes)
            else:
                self._put(img.tobytes())
            if i == n - 1:
                self.last = img.copy()


def frames_for(seconds):
    return max(1, round(float(seconds) * FPS))


def build(ws, slug, limit=None, log=print, progress=None, cancel=None, round_id=None):
    """Собирает видео. Возвращает пути к видео, обложке ролика и описанию.
    round_id (id или название тура) — пробный ролик только из этого тура, без общей заставки и финала."""
    started = time.time()
    progress = progress or (lambda frac, stage: None)
    project = ws.load(slug)
    timing, texts = project["timing"], project["texts"]
    if timing["question"] <= 0 or timing["answer"] <= 0:
        raise QuizError("Длительность вопроса и ответа должна быть больше нуля")
    rows = prepare_rows(ws, project)
    if not rows:
        raise QuizError("В квизе нет ни одного включённого трека")
    if project.get("shuffle"):
        random.shuffle(rows)
    if round_id:
        rnd = next((r for r in project["rounds"] if round_id in (r["id"], r["title"])), None)
        if not rnd:
            raise QuizError(f"Нет тура «{round_id}»")
        rows = [r for r in rows if r["round"] == rnd["id"]]
        if not rows:
            raise QuizError(f"В туре «{rnd['title']}» нет включённых треков")
    if limit:
        rows = rows[:int(limit)]
    rounds = group_rounds(project, rows)
    rows = [row for _, rs in rounds for row in rs]
    for rnd, rs in rounds:
        for row in rs:
            row["q_frames"] = frames_for(question_seconds(project, rnd))
            if row["start2"] is not None:  # два фрагмента по половине + пауза между ними
                row["q_frames"] += frames_for(SPLIT_PAUSE)
    renderer = make_renderer(ws, project)
    ring_cfg = renderer.ring.cfg

    n = len(rows)
    a_frames = frames_for(timing["answer"])
    a_samples = a_frames * SAMPLES_PER_FRAME
    vals = text_values(n, timing["question"])
    intro = (fmt(texts["intro_title"], **vals), fmt(texts["intro_subtitle"], **vals), 8)
    outro = (fmt(texts["outro_title"], **vals), fmt(texts["outro_subtitle"], **vals))

    gap_frames = round(timing["gap"] * FPS)  # пауза перед отсчётом каждого вопроса и после каждого ответа

    def card_frames(key, title):  # пустой заголовок или нулевая длительность — экрана нет
        return frames_for(timing[key]) if timing[key] > 0 and title else 0

    def add_card(key, card, narration="", music=False):
        """narration — что читает диктор; экран держится, пока он не дочитает."""
        frames, audio = card_frames(key, card[0]), None
        if not frames:
            return
        if narration.strip() and project["voice"]:
            lead = np.zeros((round(0.4 * SAMPLE_RATE), 2), dtype=np.float32)
            audio = np.concatenate([lead, speech(ws, project["voice"], narration)])
            frames = max(frames, frames_for(len(audio) / SAMPLE_RATE + 1))
        if music and project["music"]:  # фоновая музыка под голосом, каждый раз с начала
            n = frames * SAMPLES_PER_FRAME
            bed = with_fades(extract_fragment(ws.resolve(project["music"]), 0, n), 0.5, 1.5)
            bed *= float(project["music_volume"])
            if audio is not None:
                bed[:len(audio)] += audio
            audio = bed
        timeline.append(("card", frames, audio, card))

    out_dir = ws.project_dir(slug) / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{slug}_test" if limit or round_id else slug
    out = out_dir / f"{stem}.mp4"

    # 1. Обложки
    progress(0.0, "Ищу обложки")
    finder = cover_finder(ws, project)
    results = finder.find_all(rows)
    apply_covers(rows, results)
    for i, row in enumerate(rows, start=1):
        log(f"{i:>2}. {track_label(row)} — обложка: {row['cover_source']}")
    if finder.offline_reason:
        log(f"Поиск обложек в интернете не удался: {finder.offline_reason}")
    if cancel is not None and cancel.is_set():
        raise Cancelled()

    # 2. Отрывки (параллельно)
    progress(0.02, "Нарезаю отрывки")
    log("Нарезаю отрывки…")
    def cut(r):
        """(звук вопроса, отрывок для ответа, где в нём кончается вопрос)"""
        q_samples = r["q_frames"] * SAMPLES_PER_FRAME
        if r["start2"] is None:
            frag = extract_fragment(r["path"], r["start"], q_samples + a_samples)
            return with_fades(frag[:q_samples], 0.1, 0.6), frag, q_samples
        half = (r["q_frames"] - frames_for(SPLIT_PAUSE)) // 2 * SAMPLES_PER_FRAME
        frag = extract_fragment(r["path"], r["start"], half + a_samples)
        second = extract_fragment(r["path"], r["start2"], half)
        pause = np.zeros((q_samples - 2 * half, 2), dtype=np.float32)
        return (np.concatenate([with_fades(frag[:half], 0.1, 0.4), pause, with_fades(second, 0.1, 0.6)]),
                frag, half)

    with ThreadPoolExecutor(max_workers=os.cpu_count() or 2) as pool:
        fragments = list(pool.map(cut, rows))
    if cancel is not None and cancel.is_set():
        raise Cancelled()

    def answer_audio(frag, q_samples):
        mode = project["answer_audio"]
        fade = min(1.0, timing["answer"] / 3)
        if mode == "replay":
            return with_fades(frag[:a_samples], 0.15, fade)
        if mode == "continue":
            return with_fades(frag[q_samples:q_samples + a_samples], 0.3, fade)
        return None

    # 3. Сценарий
    timeline = []  # (вид, кадров, звук, данные)
    chapters_at = {}  # номер сегмента → глава в описании
    log("Озвучиваю заставки…" if project["voice"] else "Собираю сценарий…")
    if not round_id:
        add_card("intro", intro, fmt(texts["intro_narration"], **vals) or "\n".join(intro[:2]), music=True)
    first = 0
    for k, (rnd, rs) in enumerate(rounds, start=1):
        rv = text_values(len(rs), question_seconds(project, rnd), round=rnd["title"])
        idx = list(enumerate(range(first, first + len(rs)), start=1))
        first += len(rs)
        chapters_at[len(timeline)] = rnd["title"] or f"Тур {k}"
        rules = fmt(rnd["rules"], **rv)
        narration = fmt(rnd.get("narration") or "", **rv) or "\n".join(p for p in (rnd["title"], rules) if p)
        add_card("round", (rnd["title"], rules, 8), narration, music=True)
        for qn, i in idx:
            audio = np.concatenate([np.zeros((gap_frames * SAMPLES_PER_FRAME, 2), dtype=np.float32), fragments[i][0]])
            timeline.append(("question", gap_frames + rows[i]["q_frames"], audio, (i, dict(rv, n=qn))))
        card = (fmt(texts["answers_title"], **rv), fmt(texts["answers_subtitle"], **rv))
        chapters_at[len(timeline)] = card[0] or f"Ответы · {rnd['title'] or f'Тур {k}'}"
        add_card("answers_card", card, fmt(texts["answers_narration"], **rv))
        for qn, i in idx:
            audio = answer_audio(*fragments[i][1:])
            timeline.append(("answer", a_frames + gap_frames, audio, (i, dict(rv, n=qn))))
    if not round_id:
        add_card("outro", outro, fmt(texts["outro_narration"], **vals))
    total_frames = sum(seg[1] for seg in timeline)
    log(f"Квиз: {n} {plural(n, 'песня', 'песни', 'песен')}, видео {fmt_ts(total_frames / FPS)}, "
        f"{renderer.W}x{renderer.H}")

    with tempfile.TemporaryDirectory(prefix="quiz_") as tmp:
        # 4. Звуковая дорожка, точно по кадрам
        audio_path = Path(tmp) / "audio.wav"
        with wave.open(str(audio_path), "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            for _, frames_n, audio, _ in timeline:
                n_s = frames_n * SAMPLES_PER_FRAME
                chunk = np.zeros((n_s, 2), dtype=np.float32)
                if audio is not None:
                    chunk[:min(n_s, len(audio))] = audio[:n_s]
                w.writeframes((np.clip(chunk, -1, 1) * 32767).astype("<i2").tobytes())

        # 5. Видео: кадры сразу уходят в ffmpeg
        partial = out.with_suffix(".part.mp4")
        cmd = ["ffmpeg", "-v", "error", "-y",
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{renderer.W}x{renderer.H}", "-r", str(FPS), "-i", "-",
               "-i", str(audio_path), "-map", "0:v", "-map", "1:a",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(partial)]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        written = [0]
        render_start = time.time()

        def on_frame():
            written[0] += 1
            if written[0] % 15 == 0:
                frac = written[0] / total_frames
                elapsed = time.time() - render_start
                eta = elapsed / frac * (1 - frac) if frac > 0.02 else None
                stage = "Собираю видео" + (f", осталось ~{fmt_ts(eta)}" if eta else "")
                progress(0.05 + 0.93 * frac, stage)

        writer = VideoWriter(proc, cancel, on_frame)
        try:
            for kind, frames_n, audio, data in timeline:
                if kind == "card":
                    writer.segment(renderer.title_card(*data), frames_n)
                elif kind == "question":
                    i, lv = data
                    row = rows[i]
                    base = renderer.question_base(fmt(texts["question_label"], **lv),
                                                  fmt(row["question"] or texts["prompt"], **lv), row["hint"])
                    levels = analyze(audio.mean(axis=1), SAMPLE_RATE, frames_n, FPS, ring_cfg)
                    writer.segment(renderer.question_frames(base, levels, row["q_frames"], gap_frames), frames_n)
                else:
                    i, lv = data
                    writer.segment(renderer.answer_card(fmt(texts["answer_label"], **lv), rows[i]), frames_n)
            proc.stdin.close()
            err = proc.stderr.read().decode(errors="replace")
            if proc.wait() != 0:
                raise QuizError(f"ffmpeg завершился с ошибкой:\n{err.strip()}")
        except BaseException:
            proc.kill()
            proc.wait()
            partial.unlink(missing_ok=True)
            raise
        os.replace(partial, out)

    # 6. Обложка для YouTube и текст описания с таймкодами
    progress(0.99, "Готовлю обложку и описание")
    thumb = out_dir / f"{stem}_thumbnail.jpg"
    Renderer(1280, 720, project["style"], renderer.fonts,
             background=ws.resolve(project["style"].get("background"))).title_card(intro[0]).save(thumb, quality=92)

    desc_lines = [f"{intro[0]}!"] + ([intro[1]] if intro[1] else []) + [""]
    t, chapters = 0.0, []
    for seg, (_, frames_n, _, _) in enumerate(timeline):
        if seg in chapters_at:
            chapters.append((t, chapters_at[seg]))
        t += frames_n / FPS
    chapters[0] = (0.0, chapters[0][1])  # YouTube: первая глава с 0:00
    desc_lines += [f"{fmt_ts(ts)} {name}" for ts, name in chapters] + ["", "Ответы:"]
    for (rnd, rs), (_, name) in zip(rounds, chapters[::2]):
        if len(rounds) > 1:
            desc_lines.append(name)
        desc_lines += [f"{i}. {track_label(row)}" for i, row in enumerate(rs, start=1)]
    desc = out_dir / f"{stem}_description.txt"
    desc.write_text("\n".join(desc_lines) + "\n", encoding="utf-8")

    log(f"Готово за {fmt_ts(time.time() - started)}: {ws.rel(out)}")
    progress(1.0, "Готово")
    return {"video": ws.rel(out), "thumbnail": ws.rel(thumb), "description": ws.rel(desc),
            "description_text": desc.read_text(encoding="utf-8"), "duration": total_frames / FPS,
            "songs": n, "seconds": round(time.time() - started, 1)}
