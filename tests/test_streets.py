import math
import warnings

import numpy as np
import pytest

from dronemap import ar, streets
from dronemap import settings as S
from dronemap.overlay import Overlay
from dronemap.telemetry import Sample, Track

LAT0, LON0 = 46.0, 7.0
M_LAT = 1 / 111320.0
M_LON = 1 / (111320.0 * math.cos(math.radians(LAT0)))


def flight(pitch=-90.0, yaw=0.0, alt=100.0, seconds=10, hz=30):
    """A drone hovering over (LAT0, LON0), held still: telemetry repeats at the frame rate."""
    return Track(
        [Sample(i / hz, LAT0, LON0, alt, 0.0, yaw, pitch=pitch, zoom=1.0) for i in range(seconds * hz)],
        "embedded DJI telemetry",
    )


@pytest.fixture
def fake_osm(monkeypatch):
    """A north-south street through the take-off point, flat ground, no network."""
    ways = [
        {"name": "Main Street", "highway": "residential", "pts": [(LAT0 - 400 * M_LAT, LON0), (LAT0, LON0)]},
        {"name": "Main Street", "highway": "residential", "pts": [(LAT0, LON0), (LAT0 + 400 * M_LAT, LON0)]},
    ]
    monkeypatch.setattr(ar, "fetch_streets", lambda bbox: ways)
    monkeypatch.setattr(streets.Terrain, "prefetch", lambda self, *a, **k: None)
    monkeypatch.setattr(streets.Terrain, "heights", lambda self, lat, lon: np.full(np.size(lat), 250.0))
    ar._GEOM.clear()
    return ways


def opts(**over):
    o = dict(S.defaults()["streets"], enabled=True)
    o.update(over)
    return o


# ------------------------------------------------------------------ camera
def test_knots_rebuild_a_staircase_smoothly():
    t = np.arange(0, 3, 1 / 60)
    v = np.floor(t * 10) / 10  # a ramp held for 0.1 s at a time (10 Hz updates)
    out = ar._knots(t, v)
    inner = t < 2.8  # (the last reading is held to the end)
    assert np.abs(out - t)[inner].max() < 0.02  # follows the ramp...
    assert np.abs(np.diff(out, 2))[inner[2:]].max() < 1e-3  # ...without the steps


def test_gauss_has_no_lag():
    v = np.linspace(0, 10, 500)
    out = ar._gauss(v, 20)
    assert np.abs(out[100:400] - v[100:400]).max() < 1e-9


def test_camera_projection():
    W, H = 1920, 1080
    down = ar.Camera(ar.Pose(0, 0, 100, 0, -90, 1), W, H, 68)
    x, y, z = down.project(np.array([[0.0, 0.0, 0.0], [10.0, 0.0, 0.0], [0.0, 10.0, 0.0]]))
    assert (x[0], y[0], z[0]) == pytest.approx((W / 2, H / 2, 100))
    assert x[1] > W / 2  # east is right when facing north
    assert y[2] < H / 2  # north is up
    level = ar.Camera(ar.Pose(0, 0, 0, 90, 0, 1), W, H, 68)  # facing east, level
    x, y, z = level.project(np.array([[100.0, 0.0, 0.0], [100.0, 0.0, 10.0]]))
    assert (x[0], y[0], z[0]) == pytest.approx((W / 2, H / 2, 100))
    assert y[1] < y[0]  # up is up
    zoomed = ar.Camera(ar.Pose(0, 0, 100, 0, -90, 2), W, H, 68)
    east = np.array([[10.0, 0, 0]])
    assert zoomed.project(east)[0][0] - W / 2 == pytest.approx(2 * (down.project(east)[0][0] - W / 2))


def test_vertical_video_uses_long_side_for_the_lens():
    a = ar.Camera(ar.Pose(0, 0, 100, 0, -90, 1), 1920, 1080, 68)
    b = ar.Camera(ar.Pose(0, 0, 100, 0, -90, 1), 1080, 1920, 68)
    assert a.fx == pytest.approx(b.fx)


# ------------------------------------------------------------------ data
def test_merge_ways_joins_pieces_of_a_street(fake_osm):
    merged = streets.merge_ways(fake_osm + [{"name": "Side Road", "highway": "primary", "pts": [(1, 1), (1, 2)]}])
    main = [m for m in merged if m["name"] == "Main Street"]
    assert len(main) == 1 and len(main[0]["pts"]) == 3
    assert [m["major"] for m in merged if m["name"] == "Side Road"] == [True]


def test_merge_ways_handles_reversed_pieces():
    a = {"name": "A", "highway": "residential", "pts": [(0, 0), (0, 1)]}
    b = {"name": "A", "highway": "residential", "pts": [(0, 2), (0, 1)]}  # drawn the other way
    (m,) = streets.merge_ways([a, b])
    assert m["pts"] in ([(0, 0), (0, 1), (0, 2)], [(0, 2), (0, 1), (0, 0)])


def test_bbox_is_snapped_and_contains_the_flight():
    s, w, n, e = streets.bbox_around([46.0, 46.01], [7.0, 7.02], 500)
    assert s < 46.0 - 0.004 and n > 46.01 + 0.004 and w < 7.0 and e > 7.02
    assert all(abs(v / 0.005 - round(v / 0.005)) < 1e-6 for v in (s, w, n, e))


# ------------------------------------------------------------------ the layer
def test_street_line_and_name_drawn_where_the_street_is(fake_osm):
    W, H = 1280, 720
    layer = ar.StreetsLayer(flight(), opts(), S.defaults()["style"], W, H)
    layer.prefetch()
    assert layer.ready and layer.takeoff == 250.0 and layer.error is None
    img = layer.render(5.0)
    a = np.asarray(img)[..., 3]
    # looking straight down facing north: the street is a vertical line through the centre
    assert a[:, W // 2 - 2 : W // 2 + 3].max(axis=1).min() > 100
    assert a[:, : W // 2 - 200].max() == 0 and a[:, W // 2 + 200 :].max() == 0
    parts = layer.render_parts(5.0)
    assert parts[1] % 2 == 0 and parts[2] % 2 == 0


def test_names_can_be_turned_off(fake_osm):
    W, H = 1280, 720
    with_names = ar.StreetsLayer(flight(), opts(), {}, W, H)
    with_names.prefetch()
    lines_only = ar.StreetsLayer(flight(), opts(names=False), {}, W, H)
    lines_only.prefetch()
    a = np.asarray(with_names.render(5.0))[..., 3]
    b = np.asarray(lines_only.render(5.0))[..., 3]
    assert (a > 0).sum() > (b > 0).sum() + 1000  # the names around the line
    nothing = ar.StreetsLayer(flight(), opts(names=False, lines=False), {}, W, H)
    nothing.prefetch()
    assert nothing.render_parts(5.0) is None


def test_far_streets_are_not_drawn(fake_osm):
    # looking level to the north: the street runs away from the camera to 400 m
    near = ar.StreetsLayer(flight(pitch=-10, alt=30), opts(max_distance=200), {}, 1280, 720)
    near.prefetch()
    far = ar.StreetsLayer(flight(pitch=-10, alt=30), opts(max_distance=2000), {}, 1280, 720)
    far.prefetch()
    rows = lambda lay: np.flatnonzero(np.asarray(lay.render(5.0))[..., 3].max(axis=1) > 0)
    assert rows(near).min() > rows(far).min()  # the far part (higher up in the picture) is cut


def test_render_frame_matches_the_image(fake_osm):
    av = pytest.importorskip("av")
    W, H = 640, 360
    layer = ar.StreetsLayer(flight(pitch=-45), opts(), {}, W, H)
    layer.prefetch()
    ref = layer.render(5.0)
    truth = np.asarray(ref.getchannel("A")).astype(float)
    white = truth == 255
    assert white.any()
    for fmt, dtype, peak in (("yuva420p", np.uint8, 255), ("yuva420p10le", np.uint16, 1023)):
        f = layer.render_frame(5.0, fmt)
        assert (f.width, f.height, f.format.name) == (W, H, fmt)
        alpha = ar._plane_array(f.planes[3], dtype).astype(float)
        assert np.abs(alpha - truth * peak / 255).max() <= 1
        y = ar._plane_array(f.planes[0], dtype).astype(float)
        assert np.median(y[white]) > 0.8 * peak  # opaque white text and lines are bright
        # for the record: how this FFmpeg build converts the whole image in one go
        full = av.VideoFrame.from_bytes(ref.tobytes(), W, H, format="rgba").reformat(format=fmt)
        diff = np.abs(ar._plane_array(full.planes[3], dtype).astype(float) - truth * peak / 255).max()
        if diff > 1:
            warnings.warn("%s: this FFmpeg's alpha differs from the image by up to %d" % (fmt, diff), stacklevel=1)


def test_download_failure_is_reported_not_fatal(monkeypatch):
    def fail(bbox):
        raise streets.DataError("Street names couldn't be downloaded (offline).")

    monkeypatch.setattr(ar, "fetch_streets", fail)
    ar._GEOM.clear()
    layer = ar.StreetsLayer(flight(), opts(), {}, 640, 360)
    layer.prefetch()
    assert "couldn't be downloaded" in layer.error
    assert layer.render_parts(1.0) is None


def test_overlay_without_camera_data_explains_why():
    track = Track([Sample(i * 0.1, LAT0, LON0 + i * 1e-5, 50.0) for i in range(50)], "SRT captions")
    cfg = S.defaults()
    cfg["streets"]["enabled"] = True
    ov = Overlay(track, cfg, 1280, 720, offline=True)
    assert ov.streets is None and "camera angle" in ov.streets_error()


def test_overlay_draws_streets_under_the_widgets(fake_osm):
    cfg = S.defaults()
    cfg["streets"]["enabled"] = True
    cfg["tiles"]["offline"] = True
    ov = Overlay(flight(), cfg, 1280, 720)
    assert [p.name for p in ov.widgets] == ["streets", "map", "stats"]


def test_settings_clamp_street_options():
    cfg = S.defaults()
    cfg["streets"].update(max_distance=99999, text_size="big", fov=500)
    n = S.normalize(cfg)["streets"]
    assert n["max_distance"] == 3000 and n["text_size"] == 1.0 and n["fov"] == 130.0
