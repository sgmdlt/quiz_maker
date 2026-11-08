import quiz_core as qc


def _track(name, rnd):
    return {
        "id": name,
        "round": rnd,
        "path": None,
        "start": 0.0,
        "start2": None,
        "title": name,
        "artist": "Исп",
        "album": "",
        "year": "",
        "hint": "",
        "question": "",
        "answer": "",
        "answer_video": "",
    }


def test_preview_slides_follow_scenario_in_three_tries():
    p = qc.normalize(
        {
            "voice": "",
            "rounds": [
                {"id": "r1", "title": "Обычный", "rules": ""},
                {"id": "r2", "title": "Три", "rules": "", "kind": "three_tries"},
            ],
        }
    )
    rows = [_track("a", "r1")] + [_track(c, "r2") for c in "bcde"]
    picked = qc.preview_slides(p, rows, rows[2], "quiz_intro")
    assert picked["intro"].kind == "quiz_intro"
    assert picked["round"].texts["title"] == "Три"
    assert picked["question"].texts["label"] == "Вопрос 1.2 из 2"
    assert picked["answer"].kind == "triple_answer"
    assert [t["title"] for t in picked["answer"].tracks] == ["b", "c", "d"]
    assert "answers" not in picked  # в «Трёх попытках» нет заставки блока ответов


def test_preview_skips_slide_missing_from_video():
    p = qc.normalize(
        {"voice": "", "texts": {"outro_title": ""}, "rounds": [{"id": "r1", "title": "Хиты", "rules": ""}]}
    )
    rows = [_track("a", "r1")]
    picked = qc.preview_slides(p, rows, rows[0], "quiz_intro")
    assert "outro" not in picked
    assert picked["answers"].texts["title"] == "Ответы · Хиты"


def test_preview_does_not_borrow_slides_of_another_round():
    p = qc.normalize(
        {"voice": "", "rounds": [{"id": "r1", "title": "Хиты", "rules": ""}, {"id": "r2", "title": "", "rules": ""}]}
    )  # без заставки раунда
    rows = [_track("a", "r1"), _track("b", "r2")]
    picked = qc.preview_slides(p, rows, rows[1], "quiz_intro")
    assert "round" not in picked
    assert picked["answers"].texts["title"] == "Ответы · "
