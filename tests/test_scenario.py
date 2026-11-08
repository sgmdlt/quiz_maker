import quiz_core as qc
import scenario as sc


def _track(name, **kw):
    return {"id": name, "path": f"/{name}.mp3", "start": 10.0, "start2": None, "title": name, "artist": f"Исп {name}",
            "album": "", "year": "", "hint": "", "question": "", "answer": "", "answer_video": "", **kw}


def _quiz(rounds, **kw):
    return qc.normalize({"voice": "", "rounds": [{"rules": "", **r} for r in rounds], **kw})


def _slides(project, tracks_by_round, narration_seconds=lambda text: 0.0, opening="quiz_intro", scope="whole_quiz"):
    rounds = [(r, tracks_by_round[r["id"]]) for r in project["rounds"]]
    return sc.build_scenario(project, rounds, narration_seconds, opening, scope)


def test_regular_round_slide_order():
    p = _quiz([{"id": "r1", "title": "Хиты"}])
    slides = _slides(p, {"r1": [_track("a"), _track("b")]})
    assert [s.kind for s in slides] == ["quiz_intro", "rounds_list", "round_intro", "question", "question",
                                        "answers_intro", "answer", "answer", "outro"]


def test_three_tries_answer_after_each_triple():
    p = _quiz([{"id": "r1", "title": "Три попытки", "kind": "three_tries"}])
    slides = _slides(p, {"r1": [_track(c) for c in "abcdef"]})
    kinds = [s.kind for s in slides if s.kind in ("question", "triple_answer")]
    assert kinds == ["question"] * 3 + ["triple_answer"] + ["question"] * 3 + ["triple_answer"]
    labels = [s.texts["label"] for s in slides if s.kind == "question"]
    assert labels == ["Вопрос 1.1 из 2", "Вопрос 1.2 из 2", "Вопрос 1.3 из 2",
                      "Вопрос 2.1 из 2", "Вопрос 2.2 из 2", "Вопрос 2.3 из 2"]
    triples = [s for s in slides if s.kind == "triple_answer"]
    assert [[t["title"] for t in s.tracks] for s in triples] == [["a", "b", "c"], ["d", "e", "f"]]
    assert triples[1].texts["label"] == "Ответ 2 из 2"


def test_empty_title_or_zero_duration_removes_slide():
    p = _quiz([{"id": "r1", "title": "Хиты"}], texts={"answers_title": ""}, timing={"outro": 0})
    kinds = [s.kind for s in _slides(p, {"r1": [_track("a")]})]
    assert "answers_intro" not in kinds and "outro" not in kinds
    assert kinds[-1] == "answer"


def test_narration_keeps_slide_until_read():
    p = _quiz([{"id": "r1", "title": "Хиты"}], voice="ru-RU-DmitryNeural", timing={"intro": 3})
    slides = _slides(p, {"r1": [_track("a")]}, narration_seconds=lambda text: 10.0)
    intro = slides[0]
    assert intro.kind == "quiz_intro"
    assert intro.frames == round((0.4 + 10.0 + 1) * 30)  # тишина перед диктором + речь + секунда после
    assert isinstance(intro.sound[-1], sc.Narration)


def test_question_with_two_clips():
    p = _quiz([{"id": "r1", "title": "Хиты", "seconds": 10, "gap": 0}])
    q = next(s for s in _slides(p, {"r1": [_track("a", start2=60.0)]}) if s.kind == "question")
    assert q.frames == (10 + 1) * 30  # время вопроса и пауза между отрывками
    clips = [part for part in q.sound if isinstance(part, sc.Clip)]
    assert [(c.start, c.samples) for c in clips] == [(10.0, 5 * 44100), (60.0, 5 * 44100)]
    total = sum(part.samples for part in q.sound)
    assert total == q.frames * 1470


def test_chapters_start_at_zero_and_mark_answers():
    p = _quiz([{"id": "r1", "title": "Хиты"}, {"id": "r2", "title": "Кино"}])
    slides = _slides(p, {"r1": [_track("a")], "r2": [_track("b")]})
    names = [name for _, name in sc.chapters(slides)]
    assert names == ["Хиты", "Ответы · Хиты", "Кино", "Ответы · Кино"]
    assert sc.chapters(slides)[0][0] == 0.0


def test_social_cover_opens_quiz_and_round_trial_skips_shared_slides():
    p = _quiz([{"id": "r1", "title": "Хиты"}])
    tracks = {"r1": [_track("a")]}
    assert _slides(p, tracks, opening="social_cover")[0].kind == "social_cover"
    kinds = [s.kind for s in _slides(p, tracks, scope="round_trial")]
    assert kinds == ["round_intro", "question", "answers_intro", "answer"]


def test_clip_lengths_match_scenario_clips():
    p = _quiz([{"id": "r1", "title": "Хиты", "seconds": 10}, {"id": "r2", "title": "Блиц", "seconds": 6}])
    p["tracks"] = [_track("a", round="r1"), _track("b", round="r1", start2=60.0), _track("c", round="r2")]
    assert sc.clip_lengths(p) == {"a": 10.0, "b": 5.0, "c": 6.0}
