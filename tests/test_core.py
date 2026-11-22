import os
import struct
import sys

import av
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "packaging"))

from dronemap import settings as settings_mod  # noqa: E402
from dronemap.encode import render_preview, render_video  # noqa: E402
from dronemap.render import Overlay  # noqa: E402
from dronemap.telemetry import Sample, Track, _decode_djmd_frame, pb_parse, read_srt  # noqa: E402
from dronemap.tiles import world_px  # noqa: E402


# ------------------------------------------------------------------ protobuf helpers
def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _field(num, wire, payload):
    return _varint(num << 3 | wire) + payload


def _msg(num, body):
    return _field(num, 2, _varint(len(body)) + body)


def _double(num, v):
    return _field(num, 1, struct.pack("<d", v))


def _float(num, v):
    return _field(num, 5, struct.pack("<f", v))


def _int(num, v):
    return _field(num, 0, _varint(v & ((1 << 64) - 1)))


def djmd_packet(lat, lon, vx=3.0, vy=4.0, yaw_deci=1234, alt_mm=55500):
    gps = _msg(1, _double(2, lat) + _double(3, lon)) + _int(2, 96000)
    drone = (_msg(2, _float(1, vx) + _float(2, vy) + _float(3, 0.1))
             + _msg(3, _int(1, -150) + _int(2, 20) + _int(3, yaw_deci))
             + _msg(4, gps)
             + _msg(5, _float(1, float(alt_mm))))
    return _msg(3, _msg(1, _int(1, 1)) + _msg(3, drone))


def test_djmd_decode():
    s = _decode_djmd_frame(pb_parse(djmd_packet(46.25, -71.5)))
    assert s.lat == pytest.approx(46.25)
    assert s.lon == pytest.approx(-71.5)
    assert s.speed == pytest.approx(5.0)
    assert s.yaw == pytest.approx(123.4)
    assert s.alt == pytest.approx(55.5)


def test_djmd_negative_yaw_wraps():
    s = _decode_djmd_frame(pb_parse(djmd_packet(10.0, 20.0, yaw_deci=-900)))
    assert s.yaw == pytest.approx(270.0)


# ------------------------------------------------------------------ SRT
SRT_NEW = """1
00:00:00,000 --> 00:00:00,033
<font size="28">FrameCnt: 1, DiffTime: 33ms
[iso: 100] [latitude: 46.500000] [longitude: 7.500000] [rel_alt: 50.000 abs_alt: 450.0] </font>

2
00:00:01,000 --> 00:00:01,033
<font size="28">FrameCnt: 31, DiffTime: 33ms
[iso: 100] [latitude: 46.500090] [longitude: 7.500000] [rel_alt: 52.000 abs_alt: 452.0] </font>
"""

SRT_OLD = """1
00:00:00,000 --> 00:00:01,000
HOME(7.5000,46.5000) 2019.01.01 12:00:00
GPS(7.5000,46.5000,15) ISO:100 Shutter:500 EV:0 F-Num:2.8
D 0.00m, H 30.00m, HS 0.00m/s, VS 0.00m/s

2
00:00:01,000 --> 00:00:02,000
HOME(7.5000,46.5000) 2019.01.01 12:00:01
GPS(7.5001,46.5000,15) ISO:100 Shutter:500 EV:0 F-Num:2.8
D 7.66m, H 31.00m, HS 7.66m/s, VS 1.00m/s
"""


def test_srt_new_format(tmp_path):
    p = tmp_path / "a.SRT"
    p.write_text(SRT_NEW)
    t = read_srt(str(p))
    assert len(t.samples) == 2
    assert t.samples[1].t == pytest.approx(1.0)
    assert t.samples[1].alt == pytest.approx(52.0)
    # 0.00009 deg of latitude ~ 10 m in 1 s, heading north
    assert t.samples[0].speed == pytest.approx(10.0, rel=0.05)
    assert t.samples[0].yaw == pytest.approx(0.0, abs=1.0)


def test_srt_old_format(tmp_path):
    p = tmp_path / "b.srt"
    p.write_text(SRT_OLD)
    t = read_srt(str(p))
    assert [round(s.lon, 4) for s in t.samples] == [7.5, 7.5001]
    assert t.samples[0].lat == pytest.approx(46.5)
    assert t.samples[1].alt == pytest.approx(31.0)
    assert t.samples[0].yaw == pytest.approx(90.0, abs=1.0)


# ------------------------------------------------------------------ track / maths
def test_track_interpolation_and_heading_wrap():
    t = Track([Sample(0, 0.0, 0.0, 10, 1, 350), Sample(1, 0.0, 0.001, 20, 3, 10)], "x")
    s, idx, dist = t.at(0.5)
    assert idx == 0
    assert s.alt == pytest.approx(15)
    assert s.yaw == pytest.approx(0.0, abs=1e-6)   # 350 -> 10 goes through north, not south
    assert dist == pytest.approx(t.dist[-1] / 2)


def test_world_px_roundtrip_scale():
    x0, y0 = world_px(0, 0, 1)
    assert (x0, y0) == pytest.approx((256, 256))


# ------------------------------------------------------------------ rendering
def offline_cfg(**over):
    cfg = settings_mod.defaults()
    cfg["tiles"]["url"] = "placeholder"
    for k, v in over.items():
        cfg[k].update(v)
    return cfg


@pytest.mark.parametrize("orientation", ["north", "heading"])
def test_overlay_widgets_render(orientation):
    from dronemap.encode import demo_track
    cfg = offline_cfg(map={"orientation": orientation, "style": "dark"}, stats={"distance": True})
    ov = Overlay(demo_track(), cfg, 1920, 1080)
    names = [n for n, *_ in ov.widgets]
    assert names == ["map", "stats"]
    for name, w, x, y in ov.widgets:
        img = w.render(5.0)
        assert img.mode == "RGBA"
        assert img.width == w.w + 2 * w.margin and img.height == w.h + 2 * w.margin
        # corners of the widget itself must be (partly) transparent: rounded + anti-aliased
        a = img.getchannel("A")
        assert a.getpixel((w.margin, w.margin)) < 255


def test_disabled_widgets_are_skipped():
    from dronemap.encode import demo_track
    cfg = offline_cfg(map={"enabled": False})
    ov = Overlay(demo_track(), cfg, 1280, 720)
    assert [n for n, *_ in ov.widgets] == ["stats"]


def test_settings_merge_ignores_unknown_and_keeps_new_defaults():
    merged = settings_mod._merge(settings_mod.DEFAULTS, {"map": {"zoom": 15, "bogus": 1}, "nope": {}})
    assert merged["map"]["zoom"] == 15
    assert "bogus" not in merged["map"] and "nope" not in merged
    assert merged["stats"]["speed"] is True


# ------------------------------------------------------------------ end to end
@pytest.fixture(scope="module")
def sample_video(tmp_path_factory):
    import make_sample
    return make_sample.make(str(tmp_path_factory.mktemp("sample")))


def test_render_video_end_to_end(sample_video, tmp_path):
    out = tmp_path / "out.mp4"
    stages = []
    render_video(sample_video, str(out), offline_cfg(), lambda s, f: stages.append(s))
    assert "render" in stages
    with av.open(str(out)) as c:
        v = c.streams.video[0]
        assert (v.codec_context.width, v.codec_context.height) == (320, 240)
        assert sum(1 for _ in c.decode(v)) == 60
    with av.open(str(out)) as c:
        assert len(c.streams.audio) == 1


def test_render_preview(sample_video):
    img, track = render_preview(sample_video, offline_cfg(), t=1.0)
    assert img.size == (320, 240)
    assert track.source == "SRT captions"
