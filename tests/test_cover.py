from PIL import Image

import cover
import quiz_core as qc


def _solid(path, rgb):
    Image.new("RGB", (40, 40), rgb).save(path)
    return path


def test_quiz_covers_dedup_and_three_tries(tmp_path):
    ws = qc.Workspace(tmp_path)
    (tmp_path / "c").mkdir()
    files = [_solid(tmp_path / "c" / f"{i}.png", (i * 30, 0, 0)) for i in range(5)]
    rel = [f"c/{f.name}" for f in files]
    project = {
        "rounds": [{"id": "r1"}, {"id": "r2", "kind": "three_tries"}],
        "tracks": [
            {"round": "r1", "cover": rel[0]},
            {"round": "r1", "cover_auto": rel[0]},  # тот же файл — второй раз не берём
            {"round": "r1", "cover": "", "cover_auto": "", "thumb": rel[1]},
            {"round": "r1", "enabled": False, "cover": rel[4]},  # выключенный трек
            *({"round": "r2", "cover": rel[k]} for k in (2, 2, 2, 3, 3, 3)),  # по исполнителю — одна
        ],
    }
    assert cover.quiz_covers(ws, project) == [files[0], files[1], files[2], files[3]]


def test_grid_bleeds_past_frame_without_gaps():
    cols, rows, tile = cover.grid_for(74, 1920, 1080)
    assert cols * rows <= 74
    for frame, count in ((1920, cols), (1080, rows)):
        side = (count * tile - frame) / tile / 2  # вылет за край с одной стороны, в плитках
        assert 0.1 <= side <= 0.5


def test_rainbow_layout_columns_by_hue_greys_last(tmp_path):
    colors = {
        "red": (220, 30, 30),
        "orange": (250, 150, 20),
        "green": (30, 180, 60),
        "blue": (30, 60, 220),
        "white": (245, 245, 245),
        "grey": (120, 120, 120),
        "dark": (40, 40, 40),
    }
    paths = {k: _solid(tmp_path / f"{k}.png", v) for k, v in colors.items()}
    grid = cover.rainbow_layout(list(paths.values()), cols=3, rows=2)
    assert len(grid) == 6
    column = lambda c: {grid[c], grid[3 + c]}
    assert column(0) == {paths["red"], paths["orange"]}
    assert column(1) == {paths["green"], paths["blue"]}
    greys = [paths[k] for k in ("white", "grey", "dark")]
    assert column(2) <= set(greys)  # серые справа, лишняя обложка — одна из них
    assert greys.index(grid[2]) < greys.index(grid[5])  # светлое сверху
