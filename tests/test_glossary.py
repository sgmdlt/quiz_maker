import quiz_core as qc


def test_quiz_without_rounds_gets_named_round():
    # GLOSSARY.md: «Раунд», не «Тур»
    assert qc.normalize({})["rounds"][0]["title"] == "Раунд 1"


def test_round_title_falls_back_to_round_number():
    assert qc.round_title({"title": "Хиты"}, 2) == "Хиты"
    assert qc.round_title({"title": ""}, 2) == "Раунд 2"


def test_round_kind_replaces_three_tries_flag():
    p = qc.normalize({"rounds": [{"id": "a", "title": "", "rules": "", "three_tries": True},
                                 {"id": "b", "title": "", "rules": ""}]})
    assert [r["kind"] for r in p["rounds"]] == ["three_tries", "regular"]
    assert "three_tries" not in p["rounds"][0]
