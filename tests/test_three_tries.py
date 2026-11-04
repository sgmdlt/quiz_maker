import json
import subprocess

import quiz_core as qc


def test_answer_seconds_round_overrides_timing():
    project = {"timing": {"answer": 6}}
    assert qc.answer_seconds(project, {}) == 6
    assert qc.answer_seconds(project, {"answer_seconds": 4}) == 4


def test_answer_groups_and_labels():
    three = {"three_tries": True}
    items = list("abcdefg")
    assert qc.answer_groups(three, items) == [["a", "b", "c"], ["d", "e", "f"], ["g"]]
    assert qc.question_labels(three, items) == ["1.1", "1.2", "1.3", "2.1", "2.2", "2.3", "3.1"]
    assert qc.answer_groups({}, items[:2]) == [["a"], ["b"]]
    assert qc.question_labels({}, items[:2]) == ["1", "2"]


def _tone(path, freq):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration=20",
                    "-ac", "2", "-ar", str(qc.SAMPLE_RATE), str(path)], check=True)


def test_build_three_tries_round(tmp_path, monkeypatch):
    ws = qc.Workspace(tmp_path)
    tracks = []
    for k, title in enumerate(["Раз", "Два", "Три"]):
        path = tmp_path / f"{k}.wav"
        _tone(path, 300 + 100 * k)
        tracks.append({"file": path.name, "title": title, "artist": "Группа", "duration": 20.0})
    slug = ws.create("Тест", {
        "voice": "", "covers": {"online": False},
        "rounds": [{"id": "r1", "title": "Три попытки", "rules": "", "three_tries": True, "answer_seconds": 2}],
        "tracks": tracks,
    })
    t = ws.load(slug)["timing"]
    labels = []
    orig_q, orig_a = qc.Renderer.question_base, qc.Renderer.answer_group_card
    monkeypatch.setattr(qc.Renderer, "question_base", lambda self, label, *a, **k: labels.append(label) or orig_q(self, label, *a, **k))
    monkeypatch.setattr(qc.Renderer, "answer_group_card", lambda self, label, *a, **k: labels.append(label) or orig_a(self, label, *a, **k))
    result = qc.build(ws, slug, round_id="r1", log=lambda *_: None)
    assert labels[:3] == ["Вопрос 1.1 из 3", "Вопрос 1.2 из 3", "Вопрос 1.3 из 3"]
    assert set(labels[3:]) == {"Ответ 1 из 1"}
    expected = (t["round"] + 3 * (t["gap"] + t["question"]) + t["answers_card"] + 3 * 2 + t["answer_gap"])
    assert abs(result["duration"] - expected) < 0.1
    assert "1. Группа — Раз, Два, Три" in result["description_text"]
    shots = qc.preview(ws, slug, log=lambda *_: None)["images"]
    assert {"question", "answer"} <= set(shots)


def test_build_regular_round_answer_seconds(tmp_path):
    ws = qc.Workspace(tmp_path)
    for k in range(2):
        _tone(tmp_path / f"{k}.wav", 300 + 100 * k)
    slug = ws.create("Обычный", {
        "voice": "", "covers": {"online": False},
        "rounds": [{"id": "r1", "title": "Тур", "rules": "", "answer_seconds": 3}],
        "tracks": [{"file": f"{k}.wav", "title": f"П{k}", "artist": "А", "duration": 20.0} for k in range(2)],
    })
    t = ws.load(slug)["timing"]
    result = qc.build(ws, slug, round_id="r1", log=lambda *_: None)
    expected = t["round"] + 2 * (t["gap"] + t["question"]) + t["answers_card"] + 2 * (3 + t["answer_gap"])
    assert abs(result["duration"] - expected) < 0.1
    assert "1. А — П0" in result["description_text"]
