"""
spectrum_ring.py — круговой аудио-визуализатор с таймером в центре.

Столбики по кругу показывают настоящий спектр звучащего отрывка (FFT для каждого
кадра видео). Анимация на каждом треке:

    1. появление — кольцо прорисовывается по часовой стрелке (reveal_seconds);
    2. таймер    — кольцо убывает по часовой стрелке до конца отрывка;
    в центре     — обратный отсчёт секунд.

Модуль не зависит от make_quiz.py и используется так:

    cfg = RingConfig()                        # или load_config("visualizer.toml")
    ring = SpectrumRing(cfg, unit=1080, font="/путь/к/шрифту.ttf")
    levels = analyze(mono, 44100, n_frames, fps=30, cfg=cfg)   # (n_frames, ring.n_bars), 0..1
    ring.paint_static(background, center)                       # один раз на экран
    for i in range(n_frames):
        ring.draw(frame, center, levels[i], t=i / 30, duration=n_frames / 30)

Все параметры — в RingConfig ниже. Менять можно прямо здесь или в TOML-файле
(пример — visualizer.toml), который передаётся в make_quiz.py через --viz.
"""

import math
from dataclasses import dataclass, fields
from functools import lru_cache

import numpy as np
from PIL import Image, ImageColor, ImageDraw, ImageFilter, ImageFont


@dataclass
class RingConfig:
    # ── Геометрия. Размеры — в долях меньшей стороны кадра (0.1 = 108 px при 1080p) ──
    inner_radius: float = 0.16       # радиус, от которого растут столбики
    bar_length: float = 0.11         # максимальная длина столбика
    min_bar_length: float = 0.006    # длина столбика в тишине (чтобы кольцо было видно)
    bars: int = 120                  # число столбиков по кругу
    bar_width: float = 0.55          # толщина столбика: доля шага между столбиками, 0..1
    round_caps: bool = True          # скруглённые концы столбиков
    base_ring_width: float = 0.004   # толщина опорной окружности под столбиками, 0 — без неё
    base_ring_gap: float = 0.012     # зазор между опорной окружностью и столбиками
    track_opacity: float = 0.15      # видимость «отыгранной» части окружности, 0..1
    disc_opacity: float = 0.30       # затемнение круга под цифрами, 0..1

    # ── Спектр ──
    min_freq: float = 40.0           # самая низкая частота, Гц
    max_freq: float = 14000.0        # самая высокая частота, Гц
    mirror: bool = True              # зеркально: басы сверху, высокие внизу с обеих сторон
    fft_size: int = 4096             # размер окна FFT: больше — точнее басы, но плавнее реакция
    tilt_db_per_octave: float = 3.0  # подтяжка высоких частот (в музыке они тише)
    db_range: float = 42.0           # динамический диапазон: что тише — нулевой столбик
    auto_gain: bool = True           # подстраивать уровень под громкость каждого трека
    ref_db: float = -20.0            # опорный уровень, если auto_gain = false
    contrast: float = 1.5            # >1 — столбики «прыгучее», <1 — ровнее
    attack: float = 0.65             # скорость роста столбика за кадр, 0..1
    release: float = 0.2             # скорость спада столбика за кадр, 0..1
    spatial_smooth: int = 1          # сглаживание соседних столбиков, число проходов
    lookahead_ms: float = 40.0       # упреждение анализа: компенсирует сглаживание, чтобы удар
                                     # на картинке совпадал со звуком

    # ── Анимация ──
    reveal_seconds: float = 1.0      # прорисовка кольца по часовой стрелке
    reveal_easing: str = "out_cubic" # linear | out_cubic | in_out_cubic
    edge_softness: float = 10.0      # мягкость края дуги, градусов
    start_angle: float = 0.0         # где начинается дуга: градусы по часовой от 12 часов

    # ── Цвет и свечение ──
    colors: tuple = ("#FFC83D", "#FF8A5B")  # градиент сверху вниз; один цвет — без градиента
    warning_color: str = "#FF4D6D"   # цвет на последних секундах
    warning_seconds: float = 3.0     # за сколько секунд до конца краснеть, 0 — никогда
    glow: float = 0.7                # яркость свечения, 0 — без свечения
    glow_radius: float = 0.018       # размытие свечения
    supersample: int = 2             # сглаживание краёв: 1 — быстро, 2 — красиво, 3 — очень

    # ── Цифры в центре ──
    number_size: float = 1.0         # размер цифр относительно круга под ними
    number_color: str = "#FFFFFF"    # цвет цифр
    number_pulse: float = 0.12       # «прыжок» цифры при смене секунды, 0 — без него


def config_from_dict(data, source="настройках"):
    """RingConfig из словаря {параметр: значение}; неуказанные параметры — по умолчанию."""
    known = {f.name for f in fields(RingConfig)}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ValueError(f"Неизвестные параметры в {source}: {', '.join(unknown)}.\n"
                         f"Допустимые: {', '.join(sorted(known))}")
    cfg = RingConfig()
    for key, value in data.items():
        default = getattr(cfg, key)
        try:
            if isinstance(default, tuple):
                value = tuple(value) if isinstance(value, (list, tuple)) else (value,)
            elif isinstance(default, bool):
                value = bool(value)
            elif isinstance(default, int):
                value = int(value)
            elif isinstance(default, float):
                value = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"Параметр {key}: не понимаю значение {value!r}")
        setattr(cfg, key, value)
    return cfg


def load_config(path):
    """Читает TOML-файл и возвращает RingConfig с изменёнными параметрами."""
    import tomllib

    with open(path, "rb") as f:
        data = tomllib.load(f)
    return config_from_dict(data.get("ring", data), path)  # параметры в секции [ring] или без неё


def config_schema():
    """Описание параметров для интерфейса: группы, типы, значения по умолчанию и подсказки.
    Берётся прямо из комментариев в RingConfig, поэтому новый параметр сразу появится в конструкторе."""
    import inspect
    import re

    defaults = RingConfig()
    types = {f.name: f.type if isinstance(f.type, str) else f.type.__name__ for f in fields(RingConfig)}
    groups, field = [], None
    for line in inspect.getsource(RingConfig).splitlines():
        line = re.sub(r'"[^"]*"', '""', line)  # «#» внутри строк вроде "#FFC83D" — не комментарий
        head = re.match(r"\s*# ── (.+?) ──\s*$", line)
        item = re.match(r"\s*(\w+): \w+ = [^#]*(?:#\s*(.*))?$", line)
        cont = re.match(r"\s*#\s+(.*)$", line)
        if head:
            title, _, note = head.group(1).partition(". ")
            groups.append({"title": title.strip(), "note": note.strip(), "fields": []})
            field = None
        elif item and groups:
            name, help_ = item.group(1), item.group(2) or ""
            options = re.match(r"([a-z_]+(?:\s*\|\s*[a-z_]+)+)", help_)
            value = getattr(defaults, name)
            field = {
                "name": name,
                "type": types[name],
                "default": list(value) if isinstance(value, tuple) else value,
                "help": help_.strip(),
                "options": [o.strip() for o in options.group(1).split("|")] if options else None,
                "color": name == "colors" or name.endswith("_color"),
            }
            groups[-1]["fields"].append(field)
        elif cont and field is not None and not line.strip().startswith("# ──"):
            field["help"] += " " + cont.group(1).strip()
    return groups


# ───────────────────────── Анализ звука ─────────────────────────

def _band_weights(freqs, n_bands, fmin, fmax):
    """Матрица «бин FFT → полоса» с логарифмической шкалой частот."""
    edges = np.geomspace(fmin, fmax, n_bands + 1)
    centers = np.sqrt(edges[:-1] * edges[1:])
    df = freqs[1] - freqs[0]
    weights = np.zeros((len(freqs), n_bands))
    for b in range(n_bands):
        sel = (freqs >= edges[b]) & (freqs < edges[b + 1])
        if sel.sum() >= 2:
            weights[sel, b] = 1.0 / sel.sum()
        else:  # узкая полоса на басах — интерполяция между соседними бинами
            pos = centers[b] / df
            i0 = min(int(pos), len(freqs) - 2)
            frac = pos - i0
            weights[i0, b] += 1 - frac
            weights[i0 + 1, b] += frac
    return weights, centers


def analyze(mono, sample_rate, n_frames, fps, cfg):
    """Реальный спектр отрывка: уровни столбиков 0..1 для каждого кадра видео.

    mono — моно-сигнал (numpy, float), который звучит в этих кадрах.
    Возвращает массив (n_frames, число столбиков).
    """
    n = int(cfg.fft_size)
    n_bands = cfg.bars // 2 if cfg.mirror else cfg.bars
    mono = np.asarray(mono, dtype=np.float32)
    window = np.hanning(n).astype(np.float32)
    padded = np.concatenate([np.zeros(n, np.float32), mono, np.zeros(n, np.float32)])
    shift = cfg.lookahead_ms / 1000 * sample_rate
    centers = n + np.round(np.arange(n_frames) * sample_rate / fps + shift).astype(np.int64)
    idx = np.clip(centers[:, None] + np.arange(-n // 2, n // 2)[None, :], 0, len(padded) - 1)
    spectrum = np.abs(np.fft.rfft(padded[idx] * window, axis=1)) / (window.sum() / 2)
    freqs = np.fft.rfftfreq(n, 1.0 / sample_rate)

    fmax = min(cfg.max_freq, sample_rate / 2 * 0.98)
    weights, band_hz = _band_weights(freqs, n_bands, cfg.min_freq, fmax)
    db = 10 * np.log10(spectrum.astype(np.float64) ** 2 @ weights + 1e-12)
    db += cfg.tilt_db_per_octave * np.log2(band_hz / 1000.0)

    if cfg.auto_gain:
        ref = np.percentile(db, 97)
        if ref < -90:  # тишина
            levels = np.zeros_like(db)
        else:
            levels = np.clip((db - (ref - cfg.db_range)) / cfg.db_range, 0, 1)
    else:
        levels = np.clip((db - (cfg.ref_db - cfg.db_range)) / cfg.db_range, 0, 1)
    levels **= cfg.contrast

    # плавность во времени: быстрый рост, медленный спад
    out = np.empty_like(levels)
    cur = np.zeros(n_bands)
    for k in range(n_frames):
        target = levels[k]
        cur = cur + (target - cur) * np.where(target > cur, cfg.attack, cfg.release)
        out[k] = cur
    # плавность между соседними столбиками
    for _ in range(max(0, int(cfg.spatial_smooth))):
        p = np.pad(out, ((0, 0), (1, 1)), mode="edge")
        out = 0.25 * p[:, :-2] + 0.5 * p[:, 1:-1] + 0.25 * p[:, 2:]
    if cfg.mirror:
        out = np.concatenate([out, out[:, ::-1]], axis=1)
    return out


# ───────────────────────── Рисование ─────────────────────────

def _ease(name, x):
    x = min(1.0, max(0.0, x))
    if name == "linear":
        return x
    if name == "in_out_cubic":
        return 4 * x ** 3 if x < 0.5 else 1 - (-2 * x + 2) ** 3 / 2
    return 1 - (1 - x) ** 3  # out_cubic


@lru_cache(maxsize=64)
def _font(path, size):
    return ImageFont.truetype(path, max(8, int(size)))


class SpectrumRing:
    def __init__(self, cfg, unit, font):
        """font — путь к шрифту или функция size → ImageFont (для цифр в центре)."""
        self.cfg = cfg
        self.font = font if callable(font) else (lambda size, _p=str(font): _font(_p, size))
        self.ss = max(1, int(cfg.supersample))
        self.n_bars = 2 * (cfg.bars // 2) if cfg.mirror else cfg.bars

        self.r_in = cfg.inner_radius * unit
        self.max_len = cfg.bar_length * unit
        self.min_len = cfg.min_bar_length * unit
        self.base_w = cfg.base_ring_width * unit
        self.base_r = self.r_in - cfg.base_ring_gap * unit
        self.disc_r = self.base_r - self.base_w - unit * 0.008
        self.glow_r = cfg.glow_radius * unit
        self.visual_radius = self.r_in + self.max_len  # внешний край столбиков

        outer = self.visual_radius + self.glow_r * 3 + 6
        self.size = int(math.ceil(outer / 4)) * 8  # кратно 8 — для уменьшения без остатка
        self.c = self.size / 2

        self.rel = (np.arange(self.n_bars) + 0.5) * 360.0 / self.n_bars  # углы от начала дуги
        rad = np.radians(self.rel + cfg.start_angle)
        self.sin, self.cos = np.sin(rad), np.cos(rad)
        self.bar_w = max(1.0, 2 * math.pi * self.r_in / self.n_bars * cfg.bar_width)

        self.color_layer = self._gradient(cfg.colors)
        self.warn_rgb = ImageColor.getrgb(cfg.warning_color)[:3]
        self.warn_layer = Image.new("RGB", (self.size, self.size), self.warn_rgb)
        self.number_rgb = ImageColor.getrgb(cfg.number_color)[:3]
        self.number_px = self.disc_r * cfg.number_size
        self.glow_lut = [min(255, int(v * cfg.glow * 1.6)) for v in range(256)]

    # --- подготовка ---

    def _gradient(self, colors):
        stops = np.array([ImageColor.getrgb(c)[:3] for c in colors], dtype=float)
        if len(stops) == 1:
            return Image.new("RGB", (self.size, self.size), tuple(int(v) for v in stops[0]))
        yy, xx = np.mgrid[0:self.size, 0:self.size] + 0.5 - self.c
        ang = (np.degrees(np.arctan2(xx, -yy)) - self.cfg.start_angle) % 360
        t = (180 - np.abs(180 - ang)) / 180  # 0 у начала дуги → 1 напротив, симметрично
        pos = t * (len(stops) - 1)
        i0 = np.clip(pos.astype(int), 0, len(stops) - 2)
        f = (pos - i0)[..., None]
        img = stops[i0] * (1 - f) + stops[i0 + 1] * f
        return Image.fromarray(img.astype(np.uint8), "RGB")

    def box(self, center):
        x0 = int(round(center[0] - self.size / 2))
        y0 = int(round(center[1] - self.size / 2))
        return x0, y0, x0 + self.size, y0 + self.size

    def paint_static(self, img, center):
        """Неподвижная часть: тёмный круг под цифрами и бледная окружность-след."""
        ss = 4
        S = self.size * ss
        c = S / 2
        disc = Image.new("L", (S, S), 0)
        d = ImageDraw.Draw(disc)
        d.ellipse((c - self.disc_r * ss, c - self.disc_r * ss, c + self.disc_r * ss, c + self.disc_r * ss),
                  fill=int(255 * self.cfg.disc_opacity))
        track = Image.new("L", (S, S), 0)
        if self.base_w > 0 and self.cfg.track_opacity > 0:
            rb = self.base_r * ss
            ImageDraw.Draw(track).ellipse((c - rb, c - rb, c + rb, c + rb),
                                          outline=int(255 * self.cfg.track_opacity),
                                          width=max(1, round(self.base_w * ss)))
        x0, y0, _, _ = self.box(center)
        img.paste((0, 0, 0), (x0, y0), disc.reduce(ss))
        img.paste((255, 255, 255), (x0, y0), track.reduce(ss))

    def draw_sun(self, frame, center):
        """Логотип: верхняя половина кольца со столбиками «случайной» длины — с той же толщиной,
        концами, опорной дугой, цветами и свечением, что у визуализатора. center — середина основания."""
        cfg, ss = self.cfg, self.ss
        S = self.size * ss
        c = S / 2
        mask = Image.new("L", (S, S), 0)
        d = ImageDraw.Draw(mask)
        r0 = self.r_in * 0.8 * ss  # дуга меньше, чем в кольце, — лучи длиннее
        w = self.bar_w * ss
        half, width = w / 2, max(1, round(w))
        levels = np.random.default_rng(7).uniform(0.3, 1.0, self.n_bars)  # одинаковые в каждом кадре
        lengths = (self.min_len + levels * (self.visual_radius - self.r_in * 0.8 - self.min_len)) * ss
        for i in np.nonzero(self.cos > 0.05)[0]:
            r1 = r0 + lengths[i]
            sx, cy = self.sin[i], self.cos[i]
            x0, y0, x1, y1 = c + r0 * sx, c - r0 * cy, c + r1 * sx, c - r1 * cy
            d.line((x0, y0, x1, y1), fill=255, width=width)
            if cfg.round_caps:
                d.ellipse((x0 - half, y0 - half, x0 + half, y0 + half), fill=255)
                d.ellipse((x1 - half, y1 - half, x1 + half, y1 + half), fill=255)
        if self.base_w > 0:
            rb = r0 - (self.r_in - self.base_r) * ss
            d.arc((c - rb, c - rb, c + rb, c + rb), 180, 360, fill=255, width=max(1, round(self.base_w * ss)))
        if ss > 1:
            mask = mask.reduce(ss)
        x0, y0, _, _ = self.box(center)
        if cfg.glow > 0:
            g = mask.reduce(4).filter(ImageFilter.GaussianBlur(max(1.0, self.glow_r / 4)))
            g = g.resize((self.size, self.size), Image.Resampling.BILINEAR).point(self.glow_lut)
            frame.paste(self.color_layer, (x0, y0), g)
        frame.paste(self.color_layer, (x0, y0), mask)

    # --- кадр ---

    def arc_window(self, t, duration, pre=0.0):
        """Видимая дуга (от, до) в градусах от начала: сначала растёт, потом убывает.
        pre > 0 — кольцо прорисовывается и стоит полным pre секунд, отсчёт идёт после."""
        soft = max(1e-3, self.cfg.edge_softness)
        if pre > 0:
            if t < pre:
                reveal = min(self.cfg.reveal_seconds, pre)
                return -soft, _ease(self.cfg.reveal_easing, min(1.0, t / reveal) if reveal > 0 else 1.0) * (360 + soft)
            q = min(1.0, max(0.0, (t - pre) / max(1e-6, duration)))
            return q * (360 + soft) - soft, 360 + soft
        reveal = min(self.cfg.reveal_seconds, duration * 0.5)
        if reveal > 0 and t < reveal:
            return -soft, _ease(self.cfg.reveal_easing, t / reveal) * (360 + soft)
        q = min(1.0, max(0.0, (t - reveal) / max(1e-6, duration - reveal)))
        return q * (360 + soft) - soft, 360 + soft

    def draw(self, frame, center, levels, t, duration, pre=0.0):
        """Рисует кольцо и цифры на кадре (кадр меняется на месте)."""
        cfg, ss = self.cfg, self.ss
        soft = max(1e-3, cfg.edge_softness)
        a_from, a_to = self.arc_window(t, duration, pre)
        vis = np.clip((self.rel - a_from) / soft, 0, 1) * np.clip((a_to - self.rel) / soft, 0, 1)
        lengths = (self.min_len + np.asarray(levels) * self.max_len) * vis

        S = self.size * ss
        c = S / 2
        mask = Image.new("L", (S, S), 0)
        d = ImageDraw.Draw(mask)
        r0 = self.r_in * ss
        w = self.bar_w * ss
        half = w / 2
        width = max(1, round(w))
        for i in np.nonzero(lengths * ss > 0.5)[0]:
            r1 = r0 + lengths[i] * ss
            sx, cy = self.sin[i], self.cos[i]
            x0, y0, x1, y1 = c + r0 * sx, c - r0 * cy, c + r1 * sx, c - r1 * cy
            d.line((x0, y0, x1, y1), fill=255, width=width)
            if cfg.round_caps:
                d.ellipse((x0 - half, y0 - half, x0 + half, y0 + half), fill=255)
                d.ellipse((x1 - half, y1 - half, x1 + half, y1 + half), fill=255)

        # опорная окружность — только на видимой дуге
        lo, hi = max(0.0, a_from + soft / 2), min(360.0, a_to - soft / 2)
        if self.base_w > 0 and hi > lo:
            rb = self.base_r * ss
            bbox = (c - rb, c - rb, c + rb, c + rb)
            bw = max(1, round(self.base_w * ss))
            if hi - lo >= 359.9:
                d.ellipse(bbox, outline=255, width=bw)
            else:
                d.arc(bbox, lo + cfg.start_angle - 90, hi + cfg.start_angle - 90, fill=255, width=bw)

        if ss > 1:
            mask = mask.reduce(ss)

        remaining = min(duration, pre + duration - t)  # во время pre цифра стоит на полном времени
        warn = 0.0
        if cfg.warning_seconds > 0:
            warn = min(1.0, max(0.0, (cfg.warning_seconds - remaining) / 0.3))
        if warn <= 0:
            layer = self.color_layer
        elif warn >= 1:
            layer = self.warn_layer
        else:
            layer = Image.blend(self.color_layer, self.warn_layer, warn)

        x0, y0, _, _ = self.box(center)
        if cfg.glow > 0:
            g = mask.reduce(4).filter(ImageFilter.GaussianBlur(max(1.0, self.glow_r / 4)))
            g = g.resize((self.size, self.size), Image.Resampling.BILINEAR).point(self.glow_lut)
            frame.paste(layer, (x0, y0), g)
        frame.paste(layer, (x0, y0), mask)

        # цифры
        left = max(0, math.ceil(remaining - 1e-6))
        since_change = left - remaining
        scale = 1 + cfg.number_pulse * max(0.0, 1 - since_change / 0.25) ** 2
        number_warn = cfg.warning_seconds > 0 and left <= cfg.warning_seconds
        color = self.warn_rgb if number_warn else self.number_rgb
        size = int(self.number_px * scale)
        ImageDraw.Draw(frame).text(center, str(left), font=self.font(size), fill=color, anchor="mm")
