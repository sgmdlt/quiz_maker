import numpy as np

import quiz_core as qc


def _renderer(tmp_path):
    return qc.Renderer(960, 540, qc.DEFAULT_PROJECT["style"], qc.Fonts(qc.Workspace(tmp_path), {}))


def _rows(img, bg):
    diff = np.abs(np.asarray(img, dtype=int) - np.asarray(bg, dtype=int)).sum(-1)
    return np.nonzero(diff.max(1) > 60)[0]


def test_round_card_three_levels_lifted(tmp_path):
    r = _renderer(tmp_path)
    one = _rows(r.round_card("Тур", "", ""), r.bg)
    three = _rows(r.round_card("Тур", "краткое описание", "10 вопросов по 15 секунд\nпо 1 баллу"), r.bg)
    assert three.max() > one.max() + 40  # подзаголовок и правила добавляют строки
    assert (three.min() + three.max()) / 2 < 540 / 2 - 10  # весь блок выше центра


def test_new_round_has_subtitle():
    assert qc.new_round(2)["subtitle"] == ""


def test_round_card_subtitle_three_lines(tmp_path):
    r = _renderer(tmp_path)
    two = _rows(r.round_card("Тур", "раз\nдва", ""), r.bg)
    three = _rows(r.round_card("Тур", "раз\nдва\nтри", ""), r.bg)
    assert three.max() > two.max() + 15  # третья строка подзаголовка не обрезается


def test_round_card_logo_lowered(tmp_path):
    from PIL import Image
    logo = tmp_path / "logo.png"
    Image.new("RGBA", (300, 150), (255, 0, 0, 255)).save(logo)
    r = qc.Renderer(960, 540, qc.DEFAULT_PROJECT["style"], qc.Fonts(qc.Workspace(tmp_path), {}), logo=logo)
    red = lambda img: np.nonzero(((a := np.asarray(img))[..., 0] > 240) & (a[..., 1] < 20))[0].max()
    blocks = [("Тур", r.hf, 0.13, 0.06, 3, "text")]
    assert red(r.round_card("Тур", "", "")) > red(r._stack(blocks, lift=qc.ROUND_LIFT)) + 5


def test_three_tries_answer_covers_bigger(tmp_path):
    from PIL import Image
    Image.new("RGB", (400, 400), (255, 0, 0)).save(tmp_path / "c.png")
    r = _renderer(tmp_path)
    rows = [{"title": "Песня", "artist": "Исп", "album": "Альбом", "year": 2000, "cover_file": tmp_path / "c.png"}] * 3
    a = np.asarray(r.answer_group_card("Ответ", rows, 3))
    ys = np.nonzero(((a[..., 0] > 240) & (a[..., 1] < 20) & (a[..., 2] < 20)).any(1))[0]
    assert ys.max() - ys.min() + 1 >= 190  # было 164 при 960×540
