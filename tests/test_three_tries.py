import itertools
import subprocess

from test_build_media import FakeMedia

import quiz_core as qc
import scenario as sc


def test_answer_seconds_round_overrides_timing():
    project = {"timing": {"answer": 6}}
    assert sc.answer_seconds(project, {}) == 6
    assert sc.answer_seconds(project, {"answer_seconds": 4}) == 4


def test_answer_groups_and_labels():
    three = {"kind": "three_tries"}
    items = list("abcdefg")
    assert sc.answer_groups(three, items) == [["a", "b", "c"], ["d", "e", "f"], ["g"]]
    assert sc.question_labels(three, items) == ["1.1", "1.2", "1.3", "2.1", "2.2", "2.3", "3.1"]
    assert sc.answer_groups({}, items[:2]) == [["a"], ["b"]]
    assert sc.question_labels({}, items[:2]) == ["1", "2"]


def _tone(path, freq):
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={freq}:duration=20",
            "-ac",
            "2",
            "-ar",
            str(qc.SAMPLE_RATE),
            str(path),
        ],
        check=True,
    )


def test_build_three_tries_round(tmp_path, monkeypatch):
    ws = qc.Workspace(tmp_path)
    tracks = []
    for k, title in enumerate(["Раз", "Два", "Три", "Four", "Five", "Six"]):
        (tmp_path / f"{k}.wav").write_bytes(b"")
        tracks.append({"file": f"{k}.wav", "title": title, "artist": "Группа" if k < 3 else "Band", "duration": 20.0})
    slug = ws.create(
        "Тест",
        {
            "voice": "",
            "covers": {"online": False},
            "rounds": [
                {"id": "r1", "title": "Три попытки", "rules": "", "kind": "three_tries", "answer_seconds": 2, "gap": 3}
            ],
            "tracks": tracks,
        },
    )
    t = ws.load(slug)["timing"]
    labels = []
    orig_q, orig_a = qc.Renderer.question_base, qc.Renderer.answer_group_card
    monkeypatch.setattr(
        qc.Renderer, "question_base", lambda self, label, *a, **k: labels.append(label) or orig_q(self, label, *a, **k)
    )
    monkeypatch.setattr(
        qc.Renderer,
        "answer_group_card",
        lambda self, label, *a, **k: labels.append(label) or orig_a(self, label, *a, **k),
    )
    result = qc.build(ws, slug, round_id="r1", log=lambda *_: None, media=FakeMedia())
    # ответ на исполнителя сразу после его трёх вопросов, без заставки «Ответы»
    assert [k for k, _ in itertools.groupby(labels)] == [
        "Вопрос 1.1 из 2",
        "Вопрос 1.2 из 2",
        "Вопрос 1.3 из 2",
        "Ответ 1 из 2",
        "Вопрос 2.1 из 2",
        "Вопрос 2.2 из 2",
        "Вопрос 2.3 из 2",
        "Ответ 2 из 2",
    ]
    expected = t["round"] + 6 * (3 + t["question"]) + 2 * (3 * 2 + t["answer_gap"])
    assert abs(result["duration"] - expected) < 0.1
    assert "Группа" not in result["description_text"]  # ответов в описании нет, только главы


def test_build_regular_round_answer_seconds(tmp_path):  # сквозной: настоящий ffmpeg
    ws = qc.Workspace(tmp_path)
    for k in range(2):
        _tone(tmp_path / f"{k}.wav", 300 + 100 * k)
    slug = ws.create(
        "Обычный",
        {
            "voice": "",
            "covers": {"online": False},
            "rounds": [{"id": "r1", "title": "Тур", "rules": "", "answer_seconds": 3}],
            "tracks": [{"file": f"{k}.wav", "title": f"П{k}", "artist": "А", "duration": 20.0} for k in range(2)],
        },
    )
    t = ws.load(slug)["timing"]
    result = qc.build(ws, slug, round_id="r1", log=lambda *_: None)
    expected = t["round"] + 2 * (t["gap"] + t["question"]) + t["answers_card"] + 2 * (3 + t["answer_gap"])
    assert abs(result["duration"] - expected) < 0.1
    assert "Ответы Раунд 1 · Тур" in result["description_text"]
    assert "П0" not in result["description_text"]  # ответов в описании нет, только главы
