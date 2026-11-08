"""scenario.py — сценарий ролика: какие слайды идут, сколько длятся, что на них написано и что звучит.

Чистый модуль: не читает файлы, не рисует и не трогает ffmpeg. Длительность озвучки приходит снаружи
функцией narration_seconds(text), поэтому сценарий можно проверить тестами без сети и без сборки видео.
"""
from dataclasses import dataclass, field, replace

FPS = 30
SAMPLE_RATE = 44100
SAMPLES_PER_FRAME = SAMPLE_RATE // FPS  # 1470 — звук и картинка совпадают до сэмпла
SPLIT_PAUSE = 1.0                        # пауза между двумя отрывками вопроса, с
NARRATION_LEAD = 0.4                     # тишина перед озвучкой, с


# ───────────────────────── звук слайда ─────────────────────────

@dataclass(frozen=True)
class Clip:
    """Кусок трека. window — сколько сэмплов вырезается от start (громкость выравнивается по всему окну),
    из окна берётся samples сэмплов начиная с offset."""
    path: str
    start: float
    window: int
    offset: int
    samples: int
    fade_in: float
    fade_out: float


@dataclass(frozen=True)
class Silence:
    samples: int


@dataclass(frozen=True)
class Narration:
    text: str


@dataclass(frozen=True)
class VideoClip:
    """Звук видео ответа с его начала (?t= в ссылке)."""
    url: str
    samples: int
    fade_in: float
    fade_out: float


@dataclass(frozen=True)
class Slide:
    kind: str           # social_cover quiz_intro rounds_list round_intro question answers_intro answer triple_answer outro
    frames: int
    texts: dict = field(default_factory=dict)
    tracks: tuple = ()
    sound: tuple = ()   # части звука по порядку: Clip, Silence, Narration, VideoClip
    music: str = ""     # фоновая музыка слайда
    chapter: str = ""   # глава в описании YouTube, которую открывает этот слайд
    round_id: str = ""  # раунд, к которому относится слайд; у общих слайдов квиза пусто
    lead_frames: int = 0     # вопрос: пауза перед отсчётом, кольцо полное, звука нет
    track_frames: tuple = ()  # ответ на тройку вопросов: кадров на каждую песню


# ───────────────────────── правила раунда ─────────────────────────

def frames_for(seconds):
    return max(1, round(float(seconds) * FPS))


def question_seconds(project, rnd):
    """Время на вопрос: своё у раунда или общее."""
    return float(rnd.get("seconds") or project["timing"]["question"])


def answer_seconds(project, rnd):
    """Время на ответ: своё у раунда или общее. В «Трёх попытках» — на каждую песню слайда."""
    return float(rnd.get("answer_seconds") or project["timing"]["answer"])


def gap_seconds(project, rnd):
    """Пауза перед отсчётом вопроса: своя у раунда (можно 0) или общая."""
    return float(rnd["gap"] if rnd.get("gap") is not None else project["timing"]["gap"])


def is_three_tries(rnd):
    return rnd.get("kind") == "three_tries"


def answer_groups(rnd, items):
    """Что раскрывает один слайд ответа: в «Трёх попытках» — тройка вопросов, иначе один."""
    k = 3 if is_three_tries(rnd) else 1
    return [items[i:i + k] for i in range(0, len(items), k)]


def question_labels(rnd, items):
    """Номер каждого вопроса: 1, 2… или в «Трёх попытках» 1.1, 1.2, 1.3, 2.1…"""
    groups = answer_groups(rnd, items)
    if not is_three_tries(rnd):
        return [str(g) for g in range(1, len(groups) + 1)]
    return [f"{g}.{j}" for g, grp in enumerate(groups, 1) for j in range(1, len(grp) + 1)]


def round_title(rnd, n):
    return rnd["title"] or f"Раунд {n}"


def round_titles(project):
    """Названия раундов для слайда списка раундов (без номеров)."""
    return [r["title"].strip() for r in project["rounds"]]


# ───────────────────────── подписи ─────────────────────────

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


def text_values(count, seconds, n=None, total=None, round="", round_n=""):
    sec = float(seconds)
    sec_txt = f"{sec:g}"
    return {
        "n": n if n is not None else "", "total": total if total is not None else count, "count": count,
        "songs": f"{count} {plural(count, 'песня', 'песни', 'песен')}",
        "seconds": f"{sec_txt} {plural(int(sec), 'секунда', 'секунды', 'секунд') if sec == int(sec) else 'секунды'}",
        "round": round, "round_n": round_n,
    }


def round_side(texts, vals):
    """Подпись раунда слева сверху на вопросах и ответах: («РАУНД 1», название)."""
    return fmt(texts["round_label"], **vals), vals["round"].strip()


# ───────────────────────── сценарий ─────────────────────────

def build_scenario(project, rounds, narration_seconds, opening, scope):
    """Слайды ролика по порядку.
    rounds — [(раунд, его треки)] после отбора; opening — "quiz_intro" | "social_cover";
    scope — "whole_quiz" | "round_trial" (пробный раунд без заставки квиза, списка раундов и финала)."""
    timing, texts = project["timing"], project["texts"]
    slides, chapter, current_round = [], [""], [""]
    answer_gap = round(timing["answer_gap"] * FPS)  # пауза после каждого ответа

    def add(slide):
        if chapter[0]:
            slide, chapter[0] = replace(slide, chapter=chapter[0]), ""
        slides.append(replace(slide, round_id=current_round[0]))

    def intro_slide(key, kind, slide_texts, shown, narration="", music=""):
        """Слайд заставки. Пустой заголовок (shown) или нулевая длительность — слайда нет.
        Слайд держится, пока диктор не дочитает."""
        if timing[key] <= 0 or not shown:
            return
        frames, sound = frames_for(timing[key]), ()
        if narration.strip() and project["voice"]:
            sound = (Silence(round(NARRATION_LEAD * SAMPLE_RATE)), Narration(narration))
            frames = max(frames, frames_for(NARRATION_LEAD + narration_seconds(narration) + 1))
        add(Slide(kind, frames, slide_texts, sound=sound, music=music))

    def track_sound(track, q_frames, a_frames):
        """(звук вопроса, звук ответа) из отрывков трека."""
        q_s, a_s = q_frames * SAMPLES_PER_FRAME, a_frames * SAMPLES_PER_FRAME
        if track["start2"] is None:
            window, q_end = q_s + a_s, q_s
            question = (Clip(track["path"], track["start"], window, 0, q_s, 0.1, 0.6),)
        else:  # два отрывка по половине времени и пауза между ними
            half = (q_frames - frames_for(SPLIT_PAUSE)) // 2 * SAMPLES_PER_FRAME
            window, q_end = half + a_s, half
            question = (Clip(track["path"], track["start"], window, 0, half, 0.1, 0.4),
                        Silence(q_s - 2 * half),
                        Clip(track["path"], track["start2"], half, 0, half, 0.1, 0.6))
        fade = min(1.0, a_s / SAMPLE_RATE / 3)
        mode = project["answer_audio"]
        if mode == "replay":
            answer = (Clip(track["path"], track["start"], window, 0, a_s, 0.15, fade),)
        elif mode == "continue":
            answer = (Clip(track["path"], track["start"], window, q_end, a_s, 0.3, fade),)
        else:
            answer = ()
        return question, answer

    n = sum(len(rs) for _, rs in rounds)
    vals = text_values(n, timing["question"])
    if scope == "whole_quiz":
        intro = {"title": fmt(texts["intro_title"], **vals), "subtitle": fmt(texts["intro_subtitle"], **vals)}
        greeting = fmt(texts["intro_narration"], **vals) or "\n".join((intro["title"], intro["subtitle"]))
        if opening == "social_cover":
            intro_slide("cover", "social_cover", {}, "social_cover", greeting, project["intro_music"])
        else:
            intro_slide("intro", "quiz_intro", intro, intro["title"], greeting, project["intro_music"])
        titles = round_titles(project)
        joined = "\n".join(titles)
        intro_slide("rounds", "rounds_list", {"titles": titles}, joined,
             fmt(texts["rounds_narration"], **vals) or joined, project["intro_music"])

    for k, (rnd, tracks) in enumerate(rounds, start=1):
        three_tries = is_three_tries(rnd)
        current_round[0] = rnd["id"]
        q_base, a_frames = frames_for(question_seconds(project, rnd)), frames_for(answer_seconds(project, rnd))
        gap = round(gap_seconds(project, rnd) * FPS)
        rv = text_values(len(tracks), question_seconds(project, rnd), round=rnd["title"],
                         round_n=project["rounds"].index(rnd) + 1)
        groups = answer_groups(rnd, list(zip(question_labels(rnd, tracks), tracks)))
        if three_tries:  # «Вопрос 1.2 из 5»: всего — исполнителей
            rv["total"] = len(groups)

        def side_texts(label, key):
            lv = dict(rv, n=label)  # noqa: B023 (вызывается в том же витке цикла)
            label_text = fmt(texts[key], **lv)
            round_label, round_name = round_side(texts, lv)
            return {"label": label_text, "round_label": round_label, "round_title": round_name}, lv

        def question(label, track):
            q_frames = q_base + (frames_for(SPLIT_PAUSE) if track["start2"] is not None else 0)  # noqa: B023
            q_sound, a_sound = track_sound(track, q_frames, a_frames)  # noqa: B023
            slide_texts, lv = side_texts(label, "question_label")
            slide_texts |= {"prompt": fmt(track["question"] or texts["prompt"], **lv), "hint": track["hint"]}
            add(Slide("question", gap + q_frames, slide_texts, (track,),  # noqa: B023
                      (Silence(gap * SAMPLES_PER_FRAME), *q_sound), lead_frames=gap))  # noqa: B023
            return a_sound

        chapter[0] = round_title(rnd, k)
        subtitle, rules = fmt(rnd.get("subtitle") or "", **rv), fmt(rnd["rules"], **rv)
        narration = fmt(rnd.get("narration") or "", **rv) or "\n".join(p for p in (rnd["title"], subtitle, rules) if p)
        intro_slide("round", "round_intro", {"title": rnd["title"], "subtitle": subtitle, "rules": rules}, rnd["title"],
             narration, rnd.get("music", ""))

        if three_tries:  # после каждой тройки вопросов — слайд ответа, песни под свои отрывки
            for g, group in enumerate(groups, start=1):
                answer_sound = [part for label, track in group for part in question(label, track)]
                slide_texts, _ = side_texts(g, "answer_label")
                add(Slide("triple_answer", a_frames * len(group) + answer_gap, slide_texts,
                          tuple(track for _, track in group), tuple(answer_sound),
                          track_frames=(a_frames,) * len(group)))
            continue

        answer_sounds = [question(label, track) for (label, track), in groups]
        answers = {"title": fmt(texts["answers_title"], **rv), "subtitle": fmt(texts["answers_subtitle"], **rv)}
        chapter[0] = answers["title"] or f"Ответы · {round_title(rnd, k)}"
        intro_slide("answers_card", "answers_intro", answers, answers["title"],
             fmt(texts["answers_narration"], **rv), rnd.get("music", ""))
        for ((label, track),), a_sound in zip(groups, answer_sounds):
            if track["answer_video"]:
                fade = min(1.0, a_frames / FPS / 3)
                a_sound = (VideoClip(track["answer_video"], a_frames * SAMPLES_PER_FRAME, 0.15, fade),)
            slide_texts, _ = side_texts(label, "answer_label")
            add(Slide("answer", a_frames + answer_gap, slide_texts, (track,), a_sound))

    current_round[0] = ""
    if scope == "whole_quiz":
        outro = {"title": fmt(texts["outro_title"], **vals), "subtitle": fmt(texts["outro_subtitle"], **vals)}
        intro_slide("outro", "outro", outro, outro["title"], fmt(texts["outro_narration"], **vals), project["outro_music"])
    return slides


def clip_lengths(project):
    """Длина одного отрывка каждого трека в секундах — окно на волне в конструкторе.
    Те же кадры, что в сценарии: при двух отрывках каждый звучит половину времени вопроса."""
    rounds = {r["id"]: r for r in project["rounds"]}
    out = {}
    for t in project["tracks"]:
        q_frames = frames_for(question_seconds(project, rounds[t["round"]]))
        out[t["id"]] = (q_frames if t["start2"] is None else q_frames // 2) / FPS
    return out


def chapters(slides):
    """Главы для описания YouTube: [(секунда начала, название)], первая — с 0:00."""
    out, t = [], 0.0
    for s in slides:
        if s.chapter:
            out.append((t, s.chapter))
        t += s.frames / FPS
    if out:
        out[0] = (0.0, out[0][1])
    return out
