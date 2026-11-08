import numpy as np
from PIL import Image
from test_build_media import FakeMedia

import quiz_core as qc
import scenario as sc


def test_round_titles_without_numbers():
    project = {"rounds": [{"title": " Разминка"}, {"title": "OST"}, {"title": "Блиц"}]}
    assert sc.round_titles(project) == ["Разминка", "OST", "Блиц"]


def test_title_card_uses_logo_file(tmp_path):
    logo = tmp_path / "logo.png"
    Image.new("RGBA", (300, 150), (255, 0, 0, 255)).save(logo)
    fonts = qc.Fonts(qc.Workspace(tmp_path), {})
    style = qc.DEFAULT_PROJECT["style"]
    with_logo = np.asarray(qc.Renderer(960, 540, style, fonts, logo=logo).title_card("Квиз"))
    reds = (with_logo[..., 0] > 240) & (with_logo[..., 1] < 20) & (with_logo[..., 2] < 20)
    assert reds.sum() > 1000  # логотип нарисован вместо солнца
    rounds = np.asarray(qc.Renderer(960, 540, style, fonts, logo=logo).rounds_list(["Раз", "Два", "Три"]))
    assert rounds.shape == (540, 960, 3)


def test_build_starts_with_cover(tmp_path):
    ws = qc.Workspace(tmp_path)
    (tmp_path / "0.wav").write_bytes(b"")
    slug = ws.create("Обложка", {
        "voice": "", "covers": {"online": False},
        "rounds": [{"id": "r1", "title": "Тур", "rules": ""}],
        "tracks": [{"file": "0.wav", "title": "П", "artist": "А", "duration": 20.0}],
    })
    Image.new("RGB", (1920, 1080), (255, 0, 0)).save(ws.project_dir(slug) / "cover.png")
    t = ws.load(slug)["timing"]
    media = FakeMedia()
    result = qc.build(ws, slug, log=lambda *_: None, media=media)
    # обложка заменяет заставку квиза
    expected = (t["cover"] + t["rounds"] + t["round"] + t["gap"] + t["question"]
                + t["answers_card"] + t["answer"] + t["answer_gap"] + t["outro"])
    assert abs(result["duration"] - expected) < 0.1
    r, g, b = np.asarray(media.slides[0][2]).reshape(-1, 3).mean(0)
    assert r > 200 and g < 40 and b < 40  # первый кадр — обложка
