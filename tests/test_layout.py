import json

import pytest

from dronemap import settings as settings_mod
from dronemap.layout import box_to_px, migrate_box, px_to_box

FRAMES = [(3840, 2160), (1920, 1080), (2160, 3840), (1440, 1080), (1080, 1080)]


@pytest.mark.parametrize("W,H", FRAMES)
def test_round_trip(W, H):
    for rect in [(40, 30, 300, 200), (W - 340, H - 230, 300, 200), (W / 2 - 150, H - 230, 300, 200)]:
        box = px_to_box(*rect, W, H)
        assert box_to_px(box, W, H) == pytest.approx(tuple(round(v) for v in rect), abs=1)


def test_square_map_stays_square_in_portrait():
    box = settings_mod.DEFAULTS["map"]["box"]
    for W, H in FRAMES:
        x, y, w, h = box_to_px(box, W, H)
        assert abs(w - h) <= 0.02 * min(W, H), (W, H)
        assert x >= 0 and x + w <= W and y >= 0 and y + h <= H


def test_corner_anchor_is_kept_across_aspect_ratios():
    box = px_to_box(3840 - 96 - 600, 96, 600, 600, 3840, 2160)  # top-right in 16:9
    assert box["anchor"] == ["right", "top"]
    x, y, w, _h = box_to_px(box, 2160, 3840)  # same layout, vertical video
    assert x + w == pytest.approx(2160 - 96, abs=1) and y == pytest.approx(96, abs=1)


def test_centered_box_snaps_to_center_anchor():
    box = px_to_box(1920 - 400 + 30, 1800, 800, 200, 3840, 2160)
    assert box["anchor"][0] == "center"


def test_oversized_box_is_shrunk_to_fit():
    _x, _y, w, h = box_to_px({"anchor": ["left", "top"], "dx": 0, "dy": 0, "w": 3.0, "h": 1.0}, 1080, 1920)
    assert w <= 1080 and h <= 1920 and w / h == pytest.approx(3.0, rel=0.02)


def test_v1_boxes_are_migrated():
    old = [0.025, 0.566, 0.16, 0.287]  # what version 1 of the app saved
    box = migrate_box(old, {})
    assert box["anchor"] == ["left", "bottom"]
    assert box_to_px(box, 3840, 2160) == pytest.approx((96, 1223, 614, 620), abs=2)


def test_settings_normalize_and_migrate(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"map": {"zoom": "99", "box": [0.8, 0.05, 0.16, 0.287], "bogus": 1}, "nope": {}}))
    s = settings_mod.load_file(str(p))
    assert s["version"] == settings_mod.VERSION
    assert s["map"]["zoom"] == 19  # clamped
    assert s["map"]["box"]["anchor"] == ["right", "top"]
    assert "bogus" not in s["map"] and "nope" not in s
    assert s["stats"]["speed"] is True  # filled from defaults


def test_save_load_round_trip():
    s = settings_mod.defaults()
    s["style"]["accent"] = "#123456"
    settings_mod.save(s)
    assert settings_mod.load()["style"]["accent"] == "#123456"


def test_corrupt_settings_fall_back_to_defaults():
    import os

    os.makedirs(settings_mod.config_dir(), exist_ok=True)
    with open(settings_mod.settings_path(), "w") as f:
        f.write("{not json")
    assert settings_mod.load() == settings_mod.defaults()
