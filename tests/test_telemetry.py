import struct

import pytest

from dronemap.errors import NoTelemetryError
from dronemap.telemetry import Sample, Track, _decode_djmd_fast, _decode_djmd_frame, drop_glitches, load_track, pb_parse, read_srt


# ------------------------------------------------------------------ protobuf builders
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
    """A packet laid out like the DJI Mini 5 Pro's dvtm_Mini5Pro.proto frames."""
    gps = _msg(1, _double(2, lat) + _double(3, lon)) + _int(2, 96000)
    drone = (
        _msg(2, (_float(1, vx) if vx else b"") + (_float(2, vy) if vy else b"") + _float(3, 0.1))
        + _msg(3, _int(1, -150) + _int(2, 20) + (_int(3, yaw_deci) if yaw_deci else b""))
        + _msg(4, gps)
        + _msg(5, _float(1, float(alt_mm)) if alt_mm else b"")
    )
    return _msg(3, _msg(1, _int(1, 1)) + _msg(3, drone))


def test_djmd_decode():
    s = _decode_djmd_frame(pb_parse(djmd_packet(46.25, -71.5)))
    assert (s.lat, s.lon) == pytest.approx((46.25, -71.5))
    assert s.speed == pytest.approx(5.0)
    assert s.yaw == pytest.approx(123.4)
    assert s.alt == pytest.approx(55.5)


def test_djmd_negative_yaw_wraps():
    assert _decode_djmd_frame(pb_parse(djmd_packet(10.0, 20.0, yaw_deci=-900))).yaw == pytest.approx(270.0)


def test_djmd_zero_values_left_out_read_as_zero():
    # protobuf omits zero fields: landed (altitude 0), hovering (no velocity), facing north
    raw = djmd_packet(46.25, -71.5, vx=0.0, vy=0.0, yaw_deci=0, alt_mm=0)
    for s in (_decode_djmd_frame(pb_parse(raw)), _decode_djmd_fast(raw)):
        assert (s.alt, s.speed, s.yaw) == (0.0, 0.0, 0.0)


def test_djmd_no_fix_is_skipped():
    assert _decode_djmd_frame(pb_parse(djmd_packet(0.0, 0.0))) is None


def test_djmd_unknown_layout_falls_back_to_latlon_search():
    other = _msg(7, _msg(2, _int(1, 5) + _double(4, 47.123456) + _double(5, 8.654321)))
    s = _decode_djmd_frame(pb_parse(other))
    assert (s.lat, s.lon) == pytest.approx((47.123456, 8.654321))


def test_pb_parse_rejects_garbage_without_crashing():
    with pytest.raises((ValueError, IndexError)):
        pb_parse(b"\x0f\xff\xff")


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
    assert t.samples[0].speed == pytest.approx(10.0, rel=0.05)  # 0.00009 deg lat in 1 s ~ 10 m/s
    assert t.samples[0].yaw == pytest.approx(0.0, abs=1.0)  # heading north


def test_srt_old_format(tmp_path):
    p = tmp_path / "b.srt"
    p.write_text(SRT_OLD)
    t = read_srt(str(p))
    assert [round(s.lon, 4) for s in t.samples] == [7.5, 7.5001]
    assert t.samples[0].lat == pytest.approx(46.5)
    assert t.samples[1].alt == pytest.approx(31.0)
    assert t.samples[0].yaw == pytest.approx(90.0, abs=1.0)


def test_srt_windows_line_endings(tmp_path):
    p = tmp_path / "c.SRT"
    p.write_bytes(SRT_NEW.replace("\n", "\r\n").encode())
    assert len(read_srt(str(p)).samples) == 2


def test_missing_gps_gives_a_clear_error(sample_video, tmp_path):
    import shutil

    lonely = tmp_path / "no_gps.mp4"
    shutil.copy(sample_video, lonely)  # same video, but without its .SRT
    with pytest.raises(NoTelemetryError):
        load_track(str(lonely))


# ------------------------------------------------------------------ track maths
def test_interpolation_and_heading_wrap():
    t = Track([Sample(0, 0.0, 0.0, 10, 1, 350), Sample(1, 0.0, 0.001, 20, 3, 10)], "x")
    s, idx, dist = t.at(0.5)
    assert idx == 0
    assert s.alt == pytest.approx(15)
    assert s.yaw == pytest.approx(0.0, abs=1e-6)  # 350 -> 10 passes through north, not south
    assert dist == pytest.approx(t.dist[-1] / 2)


def test_at_clamps_outside_the_track():
    t = Track([Sample(1, 1.0, 1.0), Sample(2, 1.0, 1.0001)], "x")
    assert t.at(-5)[0].t == 1 and t.at(99)[0].t == 2


def test_gps_glitch_is_dropped():
    good = [Sample(i * 0.1, 46.0 + i * 1e-5, 7.0) for i in range(20)]
    glitch = Sample(1.05, 46.5, 7.5)  # ~60 km away, 0.05 s later
    kept = drop_glitches(sorted([*good, glitch], key=lambda s: s.t))
    assert glitch not in kept and len(kept) == 20


def test_glitch_filter_never_discards_most_of_a_track():
    # A drone genuinely far from its first fix (e.g. first fix was wrong): keep the data.
    s = [Sample(0, 0.0, 0.0)] + [Sample(0.1 + i * 0.1, 46.0 + i * 1e-5, 7.0) for i in range(5)]
    assert len(drop_glitches(s)) >= 5
