from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import offline_cfg

from dronemap import units
from dronemap.overlay import Overlay
from dronemap.pipeline import demo_track
from dronemap.tiles import world_px
from dronemap.widgets import _simplify, _visible_runs


@pytest.mark.parametrize("orientation", ["north", "heading"])
@pytest.mark.parametrize("style", ["standard", "dark"])
def test_widgets_render(orientation, style):
    cfg = offline_cfg(
        map={"orientation": orientation, "style": style, "show_full_route": True, "show_credit": True}, stats={"distance": True}
    )
    ov = Overlay(demo_track(), cfg, 1920, 1080)
    assert [p.name for p in ov.widgets] == ["map", "stats"]
    for p in ov.widgets:
        w = p.widget
        img = w.render(5.0)
        assert img.mode == "RGBA"
        assert img.size == (w.w + 2 * w.margin, w.h + 2 * w.margin)
        # rounded, anti-aliased corner: partly transparent, not a hard edge
        assert 0 <= img.getpixel((w.margin, w.margin))[3] < 255


def test_widgets_placed_inside_frame_for_all_aspect_ratios():
    for W, H in [(3840, 2160), (2160, 3840), (1080, 1080), (1280, 720)]:
        ov = Overlay(demo_track(), offline_cfg(), W, H)
        for p in ov.widgets:
            m = p.widget.margin
            assert -m <= p.x and p.x + p.widget.w + m <= W + m
            assert -m <= p.y and p.y + p.widget.h + m <= H + m


def test_tile_zoom_follows_output_resolution():
    for (W, H), zoom in [((3840, 2160), 17), ((1920, 1080), 16), ((7680, 4320), 18)]:
        mw = Overlay(demo_track(), offline_cfg(), W, H).widgets[0].widget
        assert mw.z == zoom and 0.7 <= mw.k <= 1.42


def test_parallel_rendering_matches_serial():
    ov = Overlay(demo_track(), offline_cfg(map={"orientation": "heading"}), 1920, 1080)
    times = [i * 0.37 for i in range(24)]
    for p in ov.widgets:
        serial = [p.widget.render(t).tobytes() for t in times]
        with ThreadPoolExecutor(6) as ex:
            parallel = [im.tobytes() for im in ex.map(p.widget.render, times)]
        assert serial == parallel


def test_disabled_and_empty_widgets_are_skipped():
    assert [p.name for p in Overlay(demo_track(), offline_cfg(map={"enabled": False}), 1280, 720).widgets] == ["stats"]
    empty = {"speed": False, "altitude": False, "heading": False, "distance": False, "coords": False}
    assert [p.name for p in Overlay(demo_track(), offline_cfg(stats=empty), 1280, 720).widgets] == ["map"]


def test_path_simplify_keeps_indices():
    pts = [(i * 0.1, 0.0) for i in range(100)]
    idx, out = _simplify(pts, 1.0)
    assert idx[0] == 0 and all(pts[i] == p for i, p in zip(idx, out))
    assert len(out) == 10


def test_visible_runs_split_offscreen_parts():
    pts = [(-500, 50), (10, 50), (90, 50), (500, 50), (900, 50), (50, 60)]
    runs = _visible_runs(pts, 100, 100, 5)
    assert len(runs) == 2
    assert runs[0][0] == (-500, 50)  # keeps the point leading into view so the line reaches the edge


def test_units():
    assert units.speed(10, "metric") == ("36", "km/h")
    assert units.speed(10, "imperial") == ("22", "mph")
    assert units.altitude(100, "imperial") == ("328", "ft")
    assert units.heading(359.7) == ("000", "°")  # never "360"
    assert units.distance(1500, "metric") == ("1.50", "km")
    assert units.distance(100, "imperial") == ("328", "ft")
    assert units.coords(-33.5, 151.25) == "33.50000° S   151.25000° E"


def test_world_px_origin():
    assert world_px(0, 0, 1) == pytest.approx((256, 256))
