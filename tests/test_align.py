"""Lining the street overlay up with the picture (align.py) and the camera heading (ar.py)."""

import math

import numpy as np
import pytest
from PIL import Image

from dronemap import align, ar, settings
from dronemap.streets import LocalFrame
from dronemap.tiles import TILE

LAT, LON = 46.25, -71.5


class FlatTerrain:
    def heights(self, lat, lon):
        return np.zeros(np.shape(lat))


def town(seed=1):
    """Ground with satellite 'imagery' of a made-up town: a street grid with blocks of
    houses of varied brightness, generated for any tile on demand."""
    g = align.Ground(LocalFrame(LAT, LON), FlatTerrain(), takeoff=0.0, offline=True)

    def fill(X, Y):
        px, py = g._px(np.ravel(X), np.ravel(Y))
        for k in {(int(a) // TILE, int(b) // TILE) for a, b in zip(px, py)}:
            if k in g.tiles:
                continue
            rng = np.random.default_rng(hash((seed,) + k) % (1 << 32))
            gx, gy = np.meshgrid(np.arange(TILE) + k[0] * TILE, np.arange(TILE) + k[1] * TILE)
            # streets every ~70 px with a slight angle, houses as random blocks between them
            road = ((gx + 0.3 * gy) % 70 < 9) | ((gy - 0.2 * gx) % 85 < 9)
            blocks = rng.integers(40, 220, (TILE // 16 + 1, TILE // 16 + 1)).repeat(16, 0).repeat(16, 1)[:TILE, :TILE]
            g.tiles[k] = np.where(road, 120, blocks).astype(np.uint8)
        g._heights_for(X, Y)

    g.fill = fill
    return g


def picture(g, B, C, fx, w, h):
    X, Y, ok = align.ground_points(g, B, C, fx, w, h)
    g.fill(X[ok], Y[ok])
    img, known = g.sample(X, Y)
    return Image.fromarray(np.where(ok & known, img, 200).astype(np.uint8))


def test_correction_round_trip():
    for pan, tilt, roll in ((2.0, -1.5, 1.0), (-4.0, 3.0, -0.5), (0.0, 0.0, 0.0)):
        assert align.angles(align.correction(pan, tilt, roll)) == pytest.approx((pan, tilt, roll), abs=1e-9)
    # tilt up raises the view, pan right turns it clockwise seen from above
    B = align.correction(1.0, 2.0, 0.0) @ align.basis(90.0, -30.0)
    assert math.degrees(math.asin(B[2, 2])) == pytest.approx(-28.0, abs=0.05)
    assert math.degrees(math.atan2(B[2, 0], B[2, 1])) > 90.0


@pytest.mark.parametrize("truth", [(2.0, -1.5, 1.0), (-5.0, 3.0, 0.0), (0.3, 0.2, -0.5)])
def test_measure_recovers_a_known_camera_error(truth):
    g = town()
    w, h = 480, 270
    fx = (w / 2) / math.tan(math.radians(69.6 / 2))
    C = np.array([0.0, 0.0, 120.0])
    B0 = align.basis(30.0, -25.0)  # what the telemetry says
    B = align.correction(*truth) @ B0  # where the camera really looked
    small = picture(g, B, C, fx, w, h)
    X, Y, ok = align.ground_points(None, B0, C, fx, w + 2 * align.MARGIN, h + 2 * align.MARGIN, step=4)
    g.fill(X[ok], Y[ok])
    D, score, other = align.measure(small, g, B0, C, fx)
    assert align.angles(D) == pytest.approx(truth, abs=0.15)
    assert align._weight(score, other) > 0.5


def test_measure_gives_up_without_ground_in_view():
    g = town()
    B0 = align.basis(0.0, 5.0)  # looking above the horizon
    assert align.measure(Image.new("L", (480, 270), 128), g, B0, np.array([0.0, 0.0, 100.0]), 330.0) is None


def test_far_rays_never_reach_far_away():
    """Rays near the horizon must not ask for imagery or terrain kilometres away."""
    B = align.basis(0.0, -1.0)
    C = np.array([0.0, 0.0, 100.0])
    X, Y, ok = align.ground_points(None, B, C, 330.0, 480, 270)
    assert ok.any() and not ok.all()
    assert np.max(np.hypot(X - C[0], Y - C[1])) < align.RANGE_M


def test_terrain_grid_stays_small_for_any_area():
    g = align.Ground(LocalFrame(LAT, LON), FlatTerrain(), takeoff=0.0, offline=True)
    g._heights_for(np.array([-4e5, 4e5]), np.array([-4e5, 4e5]))
    assert g._h[3].size <= align.MAX_HEIGHT_CELLS * 1.1


def test_alignment_drops_outliers_and_bridges_gaps():
    t = np.arange(0.0, 60.0, 2.0)
    meas = np.column_stack([t, np.full_like(t, 1.0), np.full_like(t, -0.5), np.zeros_like(t), np.ones_like(t)])
    meas[10, 1] = 8.0  # one wrong match
    meas[20:26, 4] = 0.0  # a stretch with nothing usable
    al = align.Alignment(align._clean(meas), dz=-8.0)
    assert al.at(20.0)[0] == pytest.approx(1.0, abs=0.05)
    assert al.at(46.0) == pytest.approx((1.0, -0.5, 0.0), abs=0.05)
    assert al.dz == -8.0
    again = align.Alignment.from_json(al.to_json())
    assert again.at(33.0) == pytest.approx(al.at(33.0))


def test_alignment_without_any_match_changes_nothing():
    al = align.Alignment(np.array([[1.0, 5.0, 5.0, 0.0, 0.0]]), dz=0.0)
    assert al.at(1.0) == (0.0, 0.0, 0.0)


def _heading_case(t, true):
    """Gimbal: true heading from its own zero (-45 degrees), every frame. Compass: true
    heading, 0.2 s late, 10 updates a second."""
    gimbal = true - 45.0
    late = np.interp(t - 0.2, t, true)
    held = late[(np.floor(t * 10) / 10 * 50).astype(int).clip(0, len(t) - 1)]
    return gimbal, ar._knots(t, held)


def test_heading_follows_the_gimbal_without_the_compass_lag():
    t = np.arange(0, 120, 0.02)
    true = 30 + 40 * np.clip((t - 50) / 2, 0, 1)  # a fast 40-degree turn in 2 s
    gimbal, compass = _heading_case(t, true)
    fused = ar._fuse_heading(t, compass, gimbal)
    assert fused is not None
    turn = (t > 49) & (t < 53)
    assert np.max(np.abs(fused - true)[turn]) < 0.5
    assert np.max(np.abs(compass - true)[turn]) > 3.0  # what using the compass alone would do


def test_heading_ignores_an_unrelated_gimbal_field():
    t = np.arange(0, 60, 0.02)
    true = 10 * np.sin(t / 5)
    rng = np.random.default_rng(0)
    assert ar._fuse_heading(t, true, np.cumsum(rng.normal(0, 1, len(t)))) is None


def test_heading_follows_the_gimbal_on_a_steady_orbit():
    """A circle at a constant 3.6 degrees/s: the turning rates hardly vary, so comparing
    them is just noise, but the headings still stay a fixed angle apart."""
    t = np.arange(0, 120, 0.02)  # the camera path's 50 Hz grid
    true = 50 - 3.6 * t + 0.05 * np.sin(t)
    gimbal, compass = _heading_case(t, true)
    gimbal = np.round(gimbal, 1)  # stored in tenths of a degree
    rng = np.random.default_rng(1)
    compass = compass + rng.normal(0, 0.4, len(t))  # a compass that jitters
    fused = ar._fuse_heading(t, compass, gimbal)
    assert fused is not None
    mid = (t > 20) & (t < 100)
    assert np.max(np.abs(fused - true)[mid]) < 0.5


def test_old_default_field_of_view_is_updated():
    old = settings.normalize({"streets": {"fov": 68.0}})
    assert old["streets"]["fov"] == settings.DEFAULTS["streets"]["fov"]
    assert old["streets"]["auto_align"] is True
    assert settings.normalize({"streets": {"fov": 75.0}})["streets"]["fov"] == 75.0


def test_short_clips_keep_their_length():
    """Smoothing windows longer than the clip itself (a 3 s clip, 10 s offset smoothing)."""
    v = np.linspace(0, 1, 40)
    assert len(ar._gauss(v, 100.0)) == 40
    t = np.arange(0, 3, 0.02)
    true = 20 * t
    gimbal, compass = _heading_case(t, true)
    fused = ar._fuse_heading(t, compass, gimbal)
    assert fused is not None and len(fused) == len(t)
