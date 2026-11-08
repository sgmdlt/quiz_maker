import quiz_core as qc
import server


def test_project_payload_gives_clip_length_of_each_track(tmp_path):
    ws = qc.Workspace(tmp_path)
    slug = ws.create(
        "Квиз",
        {
            "rounds": [{"id": "r1", "title": "Блиц", "rules": "", "seconds": 6}],
            "tracks": [qc.new_track(id="a", round="r1"), qc.new_track(id="b", round="r1", start2=30.0)],
        },
    )
    payload = server.App(ws).project_payload(slug)
    assert payload["clips"] == {"a": 6.0, "b": 3.0}
