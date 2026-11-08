"""Сборка ролика с тестовыми побочными эффектами: без ffmpeg, сети и диктора."""
import numpy as np
from PIL import Image

import quiz_core as qc

SR = qc.SAMPLE_RATE


def _ramp(start, samples):
    """Отрывок, по значению которого видно, откуда он вырезан: сэмпл № k трека = k / 1e7."""
    v = (round(start * SR) + np.arange(samples, dtype=np.float64)) / 1e7
    return np.repeat(v.astype(np.float32)[:, None], 2, axis=1)


class FakeMedia:
    def __init__(self):
        self.slides = []  # (кадров, звук слайда, первый кадр или None, если это поток кадров)
        self.cuts = []

    def cut(self, path, start, samples):
        self.cuts.append((str(path), start, samples))
        return _ramp(start, samples)

    def speech(self, text):
        return np.zeros((SR, 2), dtype=np.float32)

    def music(self, source, samples):
        return np.zeros((samples, 2), dtype=np.float32)

    def video(self, url):
        return "/video.mp4", 5.0

    def encoder(self, out, width, height, chunks, cancel=None, on_frame=None):
        media = self

        class Sink:
            def __init__(self):
                self.i = 0

            def segment(self, frames, n):
                media.slides.append((n, chunks[self.i], frames if isinstance(frames, Image.Image) else None))
                self.i += 1

            def close(self):
                out.write_bytes(b"")

        return Sink()


def _quiz(tmp_path, tracks, **kw):
    ws = qc.Workspace(tmp_path)
    for k, t in enumerate(tracks):
        (tmp_path / f"{k}.wav").write_bytes(b"")
        t.update(file=f"{k}.wav", duration=120.0)
    slug = ws.create("Тест", {"voice": "", "covers": {"online": False},
                              "rounds": [{"id": "r1", "title": "Раунд", "rules": "", "gap": 1}], "tracks": tracks, **kw})
    return ws, slug


def test_answer_replays_question_clip(tmp_path):
    ws, slug = _quiz(tmp_path, [{"title": "П", "artist": "А", "start": 30.0}], answer_audio="replay")
    media = FakeMedia()
    qc.build(ws, slug, round_id="r1", log=lambda *_: None, media=media)
    question, answer = media.slides[1][1], media.slides[3][1]  # заставка раунда, вопрос, «Ответы», ответ
    k = 2 * SR  # середина: затухания по краям не мешают
    assert answer[k, 0] == question[SR + k, 0] == np.float32((30 * SR + k) / 1e7)  # после паузы 1 с перед вопросом


def test_answer_continues_after_question_clip(tmp_path):
    ws, slug = _quiz(tmp_path, [{"title": "П", "artist": "А", "start": 30.0}], answer_audio="continue")
    media = FakeMedia()
    qc.build(ws, slug, round_id="r1", log=lambda *_: None, media=media)
    q_samples = ws.load(slug)["timing"]["question"] * SR
    answer = media.slides[3][1]
    k = 2 * SR
    assert answer[k, 0] == np.float32((30 * SR + q_samples + k) / 1e7)


def test_answer_silent(tmp_path):
    ws, slug = _quiz(tmp_path, [{"title": "П", "artist": "А", "start": 30.0}], answer_audio="none")
    media = FakeMedia()
    qc.build(ws, slug, round_id="r1", log=lambda *_: None, media=media)
    assert not media.slides[3][1].any()


def test_question_with_two_clips_has_pause_between(tmp_path):
    ws, slug = _quiz(tmp_path, [{"title": "П", "artist": "А", "start": 30.0, "start2": 90.0}])
    media = FakeMedia()
    qc.build(ws, slug, round_id="r1", log=lambda *_: None, media=media)
    half = ws.load(slug)["timing"]["question"] * SR // 2
    question = media.slides[1][1][SR:]  # без паузы перед вопросом
    assert question[half // 2, 0] == np.float32((30 * SR + half // 2) / 1e7)
    assert not question[half + SR // 2].any()  # пауза между отрывками
    assert question[half + SR + half // 2, 0] == np.float32((90 * SR + half // 2) / 1e7)


def test_video_answer_sounds_from_video(tmp_path):
    ws, slug = _quiz(tmp_path, [{"title": "П", "artist": "А", "start": 30.0, "answer_video": "https://youtu.be/x?t=5"}])
    media = FakeMedia()
    qc.build(ws, slug, round_id="r1", log=lambda *_: None, media=media)
    assert media.slides[3][2] is None  # поток кадров видео, а не картинка
    assert media.cuts[-1][:2] == ("/video.mp4", 5.0)
    k = 2 * SR
    assert media.slides[3][1][k, 0] == np.float32((5 * SR + k) / 1e7)
