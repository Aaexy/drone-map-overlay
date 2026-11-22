"""MP4 index reader, telemetry cache, local staging, and path simplification."""

import math
import os

import av
import pytest
from conftest import offline_cfg

from dronemap import mp4index, pipeline, telemetry
from dronemap.telemetry import Sample, Track
from dronemap.widgets import _chunks, _rdp, _simplify_path


# ------------------------------------------------------------------ MP4 index
def test_mp4_index_matches_ffmpeg(sample_video):
    tracks = mp4index.tracks(sample_video)
    video = next(t for t in tracks if t.handler == b"vide")
    assert video.format in (b"avc1", b"mp4v")
    with av.open(sample_video) as c:
        packets = [bytes(p) for p in c.demux(c.streams.video[0]) if p.size]
    ours = [data for _, data in mp4index.read_samples(sample_video, video)]
    assert ours == packets


def test_mp4_index_on_non_mp4(tmp_path):
    p = tmp_path / "junk.mp4"
    p.write_bytes(b"\x00" * 100)
    assert mp4index.tracks(str(p)) == []


# ------------------------------------------------------------------ telemetry cache
def test_embedded_telemetry_is_cached(sample_video, monkeypatch):
    calls = {"n": 0}
    samples = [Sample(i * 0.1, 46.0 + i * 1e-5, 7.0, 50.0, 3.0, 90.0) for i in range(30)]

    def fake_read(path):
        calls["n"] += 1
        return Track([Sample(s.t, s.lat, s.lon, s.alt, s.speed, s.yaw) for s in samples], "embedded DJI telemetry")

    monkeypatch.setattr(telemetry, "read_embedded", fake_read)
    first = telemetry.load_track(sample_video)
    second = telemetry.load_track(sample_video)
    assert calls["n"] == 1
    assert [(s.t, s.lat, s.yaw) for s in first.samples] == [(s.t, s.lat, s.yaw) for s in second.samples]


def test_cache_invalidated_when_file_changes(sample_video, tmp_path, monkeypatch):
    import shutil

    copy = tmp_path / "clip.mp4"
    shutil.copy(sample_video, copy)
    shutil.copy(sample_video[:-4] + ".SRT", str(copy)[:-4] + ".SRT")
    a = telemetry._cache_file(str(copy))
    os.utime(copy, (1_000_000_000, 1_000_000_000))
    assert telemetry._cache_file(str(copy)) != a


# ------------------------------------------------------------------ staging
def test_render_via_local_staging(sample_video, tmp_path, monkeypatch):
    stage = tmp_path / "stage"
    stage.mkdir()
    monkeypatch.setattr(pipeline, "_staging_dir", lambda folder, needed: str(stage))
    stages = []
    out = tmp_path / "out" / "result.mp4"
    res = pipeline.render_video(sample_video, str(out), offline_cfg(), lambda s, f: stages.append(s))
    with av.open(res.output) as c:
        assert sum(1 for _ in c.decode(c.streams.video[0])) == 60  # complete, valid file
    assert "saving" in stages and "finishing" in stages
    assert os.listdir(stage) == []  # staging file removed
    assert os.listdir(out.parent) == ["result.mp4"]  # no .part left behind


def test_no_staging_on_the_same_drive(tmp_path):
    import tempfile

    assert pipeline._staging_dir(tempfile.gettempdir(), 1) is None


# ------------------------------------------------------------------ path simplification
def _dist_to_segment(p, a, b):
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    u = 0 if L2 == 0 else max(0, min(1, ((p[0] - ax) * dx + (p[1] - ay) * dy) / L2))
    return math.hypot(p[0] - ax - u * dx, p[1] - ay - u * dy)


def test_simplified_path_stays_within_tolerance():
    pts = [(i, 30 * math.sin(i / 25) + (0.05 if i % 2 else 0)) for i in range(800)]
    eps = 0.33
    idx, kept = _simplify_path(pts, 0.5, eps)
    assert len(kept) < len(pts) / 4
    assert (idx[0] == 0 and idx[-1] == len(pts) - 1) or idx[-1] >= len(pts) - 2
    for i, p in enumerate(pts):
        j = max(0, min(len(idx) - 2, next((k for k, v in enumerate(idx) if v > i), len(idx)) - 1))
        d = min(_dist_to_segment(p, kept[j], kept[j + 1]), _dist_to_segment(p, kept[max(0, j - 1)], kept[j]))
        assert d <= eps + 0.5 + 1e-6  # RDP tolerance + min-step thinning


def test_rdp_keeps_corners():
    square = [(x, 0) for x in range(10)] + [(9, y) for y in range(1, 10)] + [(x, 9) for x in range(8, -1, -1)]
    kept = [square[i] for i in _rdp(square, 0.1)]
    assert kept == [(0, 0), (9, 0), (9, 9), (0, 9)]


def test_chunks_cover_the_path_with_overlap():
    pts = [(float(i), 0.0) for i in range(200)]
    ch = _chunks(pts, size=48)
    assert ch[0][0] == 0 and ch[-1][1] == 199
    assert all(a[1] == b[0] for a, b in zip(ch, ch[1:]))
    assert all(c[2] <= c[4] for c in ch)


@pytest.mark.parametrize("t", [5.0, 15.0, 29.0])
def test_long_trail_draws_same_as_before_chunking(t):
    """Chunk culling must not drop visible parts of the trail."""
    from dronemap.overlay import Overlay
    from dronemap.pipeline import demo_track

    m = Overlay(demo_track(), offline_cfg(map={"trail": "full"}), 1920, 1080).widgets[0].widget
    with_culling = m.render(t)
    m.path_chunks = [(0, len(m.path_pts) - 1, -1e18, -1e18, 1e18, 1e18)]  # one chunk: always "visible"
    without = m.render(t)
    assert with_culling.tobytes() == without.tobytes()


# ------------------------------------------------------------------ tile downloads
def test_failed_tiles_are_not_retried_every_frame(monkeypatch):
    import time

    from dronemap import tiles

    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        raise OSError("network is unreachable")

    monkeypatch.setattr(tiles.urllib.request, "urlopen", boom)
    src = tiles.TileSource("https://tiles.invalid/{z}/{x}/{y}.png", 12)
    t0 = time.time()
    for _ in range(50):
        src.region(0, 0, 600, 600)  # 9 tiles, over and over (like rendering frames)
    assert calls["n"] == 9 and src.failed == 9
    assert "unreachable" in src.last_error
    assert time.time() - t0 < 2


def test_downloads_use_bundled_certificates():
    import certifi

    from dronemap import tiles

    ctx = tiles.ssl_context()
    assert ctx.verify_mode.name == "CERT_REQUIRED"
    assert ctx.cert_store_stats()["x509_ca"] > 50  # loaded from certifi's bundle
    assert certifi.where()
