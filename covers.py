"""
covers.py — поиск обложек релизов для экранов ответа.

Порядок поиска для каждой песни:
    1. обложка, загруженная вручную (в конструкторе или колонкой cover);
    2. обложка, вшитая в аудиофайл (mp3, m4a, flac), если она не меньше 400 px;
    3. ранее найденная — кэш в library/covers/auto/;
    4. поиск в интернете: Deezer, затем iTunes (без ключей и регистрации);
    5. миниатюра видео с YouTube (чёрные поля обрезаются) — у треков из YouTube Music
       это и есть обложка, у клипов — кадр из видео;
    6. вшитая обложка маленького размера;
    7. ничего не нашлось — на экране ответа будет нарисованная пластинка.

Альбом и год берутся из таблицы/конструктора, иначе из тегов файла, иначе из интернета.
"""

import hashlib
import io
import json
import re
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from pathlib import Path

from PIL import Image, ImageChops

USER_AGENT = "quiz-maker/1.0 (+music quiz video generator)"
TIMEOUT = 12
MIN_GOOD_SIZE = 400
COMPILATION = re.compile(
    r"greatest hits|best of|the best|collection|anthology|essentials|hits\b|"
    r"сборник|лучшее|лучшие песни|хиты", re.IGNORECASE)


# ───────────────────────── сравнение названий ─────────────────────────

def _norm(s):
    s = (s or "").lower().replace("ё", "е")
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)
    s = re.sub(r"\b(feat|ft|featuring)\b.*", " ", s)
    s = re.sub(r"[^\w\s]", " ", s)
    return " ".join(s.split())


def similar(a, b):
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def pick_best(candidates, artist, title):
    """Из результатов поиска выбирает подходящий: совпадают исполнитель и название,
    оригинальный альбом предпочтительнее сборника."""
    good = [c for c in candidates
            if c.get("url") and similar(c["artist"], artist) >= 0.6 and similar(c["title"], title) >= 0.5]
    if not good:
        return None
    regular = [c for c in good if not COMPILATION.search(c.get("album") or "")]
    return (regular or good)[0]


# ───────────────────────── интернет ─────────────────────────

def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read()


def _get_json(url):
    return json.loads(_get(url).decode("utf-8"))


def parse_deezer(data):
    out = []
    for it in (data or {}).get("data") or []:
        album = it.get("album") or {}
        if not album.get("md5_image") and not album.get("cover_xl"):
            continue
        out.append({
            "artist": (it.get("artist") or {}).get("name", ""),
            "title": it.get("title", ""),
            "album": album.get("title", ""),
            "album_id": album.get("id"),
            "url": album.get("cover_xl") or album.get("cover_big"),
            "source": "Deezer",
        })
    return out


def search_deezer(artist, title):
    base = "https://api.deezer.com/search?"
    data = _get_json(base + urllib.parse.urlencode({"q": f'artist:"{artist}" track:"{title}"', "limit": 15}))
    cands = parse_deezer(data)
    if not cands:
        data = _get_json(base + urllib.parse.urlencode({"q": f"{artist} {title}", "limit": 15}))
        cands = parse_deezer(data)
    best = pick_best(cands, artist, title)
    if best and best.get("album_id"):
        try:
            best["year"] = (_get_json(f"https://api.deezer.com/album/{best['album_id']}").get("release_date") or "")[:4]
        except Exception:
            pass
    return best


def parse_itunes(data):
    out = []
    for it in (data or {}).get("results") or []:
        art = it.get("artworkUrl100")
        if not art:
            continue
        out.append({
            "artist": it.get("artistName", ""),
            "title": it.get("trackName", ""),
            "album": it.get("collectionName", ""),
            "url": art.replace("100x100bb", "1000x1000bb"),
            "url_fallback": art.replace("100x100bb", "600x600bb"),
            "year": (it.get("releaseDate") or "")[:4],
            "source": "iTunes",
        })
    return out


def search_itunes(artist, title, country="US"):
    q = urllib.parse.urlencode({"term": f"{artist} {title}", "entity": "song", "limit": 25, "country": country})
    return pick_best(parse_itunes(_get_json("https://itunes.apple.com/search?" + q)), artist, title)


def download_image(urls, dest):
    for url in urls:
        if not url:
            continue
        try:
            img = Image.open(io.BytesIO(_get(url))).convert("RGB")
        except Exception:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        img.save(dest, quality=93)
        return True
    return False


# ───────────────────────── файл ─────────────────────────

def extract_embedded(audio, dest):
    """Достаёт вшитую обложку. Возвращает размер (меньшую сторону) или 0."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(audio), "-an", "-map", "0:v:0?",
                            "-frames:v", "1", str(dest)], capture_output=True)
        if r.returncode != 0:
            dest.unlink(missing_ok=True)
    if not dest.exists() or dest.stat().st_size == 0:
        dest.unlink(missing_ok=True)
        return 0
    try:
        with Image.open(dest) as im:
            return min(im.size)
    except Exception:
        return 0


def trim_borders(src, dest, threshold=24):
    """Обрезает чёрные (или однотонные) поля у миниатюры видео и сохраняет результат."""
    img = Image.open(src).convert("RGB")
    corner = img.getpixel((0, 0))
    diff = ImageChops.difference(img, Image.new("RGB", img.size, corner)).convert("L")
    box = diff.point(lambda v: 255 if v > threshold else 0).getbbox()
    if box and (box[2] - box[0]) > img.width * 0.3 and (box[3] - box[1]) > img.height * 0.3:
        img = img.crop(box)
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, quality=93)
    return dest


def read_tags(audio):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format_tags:stream_tags",
                        "-of", "json", str(audio)], capture_output=True, text=True)
    try:
        data = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return "", ""
    tags = {}
    for src in [data.get("format", {})] + data.get("streams", []):
        for k, v in (src.get("tags") or {}).items():
            tags.setdefault(k.lower(), v)
    year = re.search(r"\d{4}", tags.get("date") or tags.get("year") or tags.get("originaldate") or "")
    return tags.get("album", "").strip(), year.group(0) if year else ""


def slug(artist, title):
    name = f"{artist} - {title}" if artist else title
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip(" .")[:120] or "cover"


def _file_key(path):
    p = Path(path)
    try:
        st = p.stat()
        raw = f"{p.resolve()}|{st.st_size}|{int(st.st_mtime)}"
    except OSError:
        raw = str(p)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


# ───────────────────────── поиск для списка песен ─────────────────────────

class CoverFinder:
    """track — словарь с ключами: path (аудио), artist, title и необязательными
    cover (своя картинка), thumb (миниатюра YouTube), album, year."""

    def __init__(self, cache_dir, online=True, itunes_country="US"):
        self.cache_dir = Path(cache_dir)
        self.online = online
        self.country = itunes_country or "US"
        self.meta_path = self.cache_dir / "covers.json"
        self.meta = {}
        if self.meta_path.is_file():
            try:
                self.meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self.meta = {}
        self._lock = threading.Lock()
        self.offline_reason = None

    def _search_online(self, artist, title):
        for search in (search_deezer, lambda a, t: search_itunes(a, t, self.country)):
            if not self.online:
                return None
            try:
                found = search(artist, title)
            except urllib.error.HTTPError:  # сервис ответил ошибкой — пробуем следующий
                continue
            except urllib.error.URLError as e:  # нет связи — больше не пытаемся
                with self._lock:
                    self.online = False
                    self.offline_reason = str(e.reason)
                return None
            except Exception:
                continue
            if found:
                return found
        return None

    def cached_file(self, artist, title):
        path = self.cache_dir / f"{slug(artist, title)}.jpg"
        return path if path.is_file() else None

    def forget(self, artist, title):
        """Удаляет найденную в интернете обложку, чтобы поискать заново."""
        key = slug(artist, title)
        (self.cache_dir / f"{key}.jpg").unlink(missing_ok=True)
        with self._lock:
            self.meta.pop(key, None)

    def find(self, track):
        """Возвращает dict: file (Path или None), source (откуда), album, year."""
        info = {"file": None, "source": "не найдена — будет пластинка",
                "album": track.get("album", ""), "year": track.get("year", "")}
        audio = track.get("path")
        if audio and Path(audio).is_file():
            tag_album, tag_year = read_tags(audio)
            info["album"] = info["album"] or tag_album
            info["year"] = info["year"] or tag_year

        if track.get("cover") and Path(track["cover"]).is_file():
            info.update(file=Path(track["cover"]), source="своя")
            return info

        emb_size, embedded = 0, None
        if audio and Path(audio).is_file():
            embedded = self.cache_dir / "embedded" / f"{_file_key(audio)}.png"
            emb_size = extract_embedded(audio, embedded)
            if emb_size >= MIN_GOOD_SIZE:
                info.update(file=embedded, source="из файла")
                return info

        artist, title = track.get("artist", ""), track.get("title", "")
        key = slug(artist, title)
        cached = self.cache_dir / f"{key}.jpg"
        meta = self.meta.get(key, {})
        if cached.is_file():
            info.update(file=cached, source=f"найдена ранее ({meta.get('source', 'интернет')})")
            info["album"] = info["album"] or meta.get("album", "")
            info["year"] = info["year"] or meta.get("year", "")
            return info

        if self.online and artist and title:
            found = self._search_online(artist, title)
            if found and download_image([found.get("url"), found.get("url_fallback")], cached):
                with self._lock:
                    self.meta[key] = {"album": found.get("album", ""), "year": found.get("year", ""),
                                      "source": found["source"], "found_as": f"{found['artist']} — {found['title']}"}
                info.update(file=cached, source=found["source"])
                info["album"] = info["album"] or found.get("album", "")
                info["year"] = info["year"] or found.get("year", "")
                return info

        thumb = track.get("thumb")
        if thumb and Path(thumb).is_file():
            trimmed = self.cache_dir / "youtube" / f"{Path(thumb).stem}.jpg"
            try:
                if not trimmed.is_file():
                    trim_borders(thumb, trimmed)
                info.update(file=trimmed, source="миниатюра YouTube")
                return info
            except Exception:
                pass

        if emb_size:
            info.update(file=embedded, source=f"из файла (маленькая, {emb_size} px)")
        return info

    def save_meta(self):
        if self.meta:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            with self._lock:
                data = json.dumps(self.meta, ensure_ascii=False, indent=2)
            self.meta_path.write_text(data, encoding="utf-8")

    def find_all(self, tracks, on_done=None):
        def one(item):
            i, t = item
            res = self.find(t)
            if on_done:
                on_done(i, res)
            return res

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(one, enumerate(tracks)))
        self.save_meta()
        return results
