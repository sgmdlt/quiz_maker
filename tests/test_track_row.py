import pytest

import quiz_core as qc


def test_track_row_has_everything_slides_need(tmp_path):
    ws = qc.Workspace(tmp_path)
    t = qc.new_track(title="", artist="Исп", start=12.5, start2=40.0, hint="подсказка", file="library/youtube/abc.webm")
    row = qc.track_row(ws, t)
    assert (row["start"], row["start2"], row["hint"]) == (12.5, 40.0, "подсказка")
    assert row["title"] == "abc"  # пустое название — имя файла, как в ролике


def test_build_rows_reject_track_not_downloaded(tmp_path):
    ws = qc.Workspace(tmp_path)
    project = {"tracks": [qc.new_track(title="Песня", file="library/youtube/none.webm")]}
    with pytest.raises(qc.QuizError, match="ещё не скачан"):
        qc.prepare_rows(ws, project)
