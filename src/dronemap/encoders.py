"""Choosing and opening a video encoder that works on this machine."""

from __future__ import annotations

import logging
import os
import sys
from fractions import Fraction
from typing import NamedTuple

import av

log = logging.getLogger(__name__)


class Candidate(NamedTuple):
    codec: str
    pix_fmt: str
    hardware: bool


def nominal_rate(stream) -> Fraction:
    """The frame rate the camera was set to (e.g. 60000/1001). Not the average, which
    becomes an odd fraction when the camera dropped frames."""
    for r in (getattr(stream, "guessed_rate", None), getattr(stream, "base_rate", None), stream.average_rate):
        if r and 1 <= float(r) <= 1000:
            return Fraction(r)
    return Fraction(30)


def _available(name: str) -> bool:
    try:
        av.codec.Codec(name, "w")
        return True
    except Exception:
        return False


def candidates(src_codec: str, preference: str, ten_bit: bool) -> list[Candidate]:
    """Encoders to try, best first. "auto" keeps HEVC sources in HEVC (10-bit when the
    source is); "h264" favours compatibility."""
    out: list[Candidate] = []
    if preference == "auto" and src_codec in ("hevc", "h265"):
        if sys.platform == "darwin":
            out.append(Candidate("hevc_videotoolbox", "p010le" if ten_bit else "nv12", True))
        out.append(Candidate("libx265", "yuv420p10le" if ten_bit else "yuv420p", False))
    if sys.platform == "darwin":
        out.append(Candidate("h264_videotoolbox", "nv12", True))
    out += [
        Candidate("libx264", "yuv420p", False),
        Candidate("libopenh264", "yuv420p", False),
        Candidate("mpeg4", "yuv420p", False),
    ]
    return [c for c in out if _available(c.codec)]


def _configure(st, src_stream, cand: Candidate, width: int, height: int) -> None:
    rate = nominal_rate(src_stream)
    src_cc = src_stream.codec_context
    bitrate = src_stream.bit_rate or src_cc.bit_rate or int(width * height * float(rate) * 0.15)
    st.width, st.height, st.pix_fmt = width, height, cand.pix_fmt
    # Encode in the source's own time base so every timestamp maps exactly. A time base of
    # 1/average_rate (e.g. 1340339/80130000 after dropped frames) makes neighbouring frames
    # round to the same timestamp, which the MP4 muxer rejects.
    st.time_base = src_stream.time_base
    cc = st.codec_context
    cc.time_base = src_stream.time_base
    cc.framerate = rate
    threads = str(max(1, (os.cpu_count() or 4) - 2))
    if cand.codec == "libx265":
        cc.options = {"crf": "20", "preset": "fast", "x265-params": "log-level=error:pools=" + threads}
    elif cand.codec == "libx264":
        cc.options = {"crf": "18", "preset": "fast", "threads": threads}
    else:
        cc.bit_rate = int(bitrate)
    if cand.codec.endswith("_videotoolbox"):
        # Without this, VideoToolbox paces itself to real time (~60 fps at 4K). Measured on
        # DJI 4K/60 10-bit footage at source bitrate: 1.7x faster, SSIM 0.9835 vs 0.9840.
        cc.options = {"prio_speed": "1"}
    if cand.codec in ("hevc_videotoolbox", "libx265"):
        st.codec_tag = "hvc1"  # plays in QuickTime / Photos / iOS
    for attr in ("color_primaries", "color_trc", "colorspace", "color_range"):
        try:
            setattr(cc, attr, getattr(src_cc, attr))
        except Exception:
            pass


def open_output(path: str, src_stream, preference: str, width: int, height: int) -> tuple[object, object, Candidate]:
    """Open `path` with the first encoder that actually works here.
    Returns (container, video_stream, candidate)."""
    src_cc = src_stream.codec_context
    ten_bit = "10" in (src_cc.format.name if src_cc.format else "")
    errors = []
    for cand in candidates(src_cc.name, preference, ten_bit):
        # No "+faststart": it rewrites the whole finished file to move the index to the
        # front (only useful for web streaming) and took minutes on SD cards.
        out = av.open(path, "w", format="mp4")
        try:
            st = out.add_stream(cand.codec, rate=nominal_rate(src_stream))
            _configure(st, src_stream, cand, width, height)
            st.codec_context.open()
            log.info("Encoder: %s (%s)", cand.codec, cand.pix_fmt)
            return out, st, cand
        except Exception as e:  # e.g. no hardware encoder on this machine: try the next one
            log.info("Encoder %s unavailable: %s", cand.codec, e)
            errors.append("%s: %s" % (cand.codec, e))
            out.close()
            try:
                os.remove(path)
            except OSError:
                pass
    raise RuntimeError("No usable video encoder. " + "; ".join(errors))
