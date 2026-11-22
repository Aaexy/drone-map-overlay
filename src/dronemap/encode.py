"""Decode a video, burn the overlay in, and encode the result (all via PyAV/FFmpeg)."""
from __future__ import annotations

import os
import sys
import time
from fractions import Fraction
from typing import Callable, Optional

import av
from av.video.frame import VideoFrame
from PIL import Image

from . import settings as settings_mod
from .render import Overlay
from .telemetry import load_track


class Cancelled(Exception):
    pass


def main_video_stream(container):
    vids = [s for s in container.streams.video
            if not (s.disposition & av.stream.Disposition.attached_pic)]
    if not vids:
        raise ValueError("No video stream in this file.")
    return max(vids, key=lambda s: (s.codec_context.width or 0) * (s.codec_context.height or 0))


def output_path(src: str, cfg: dict) -> str:
    folder = cfg["output"].get("folder") or os.path.dirname(os.path.abspath(src))
    base = os.path.splitext(os.path.basename(src))[0] + (cfg["output"].get("suffix") or "_map")
    p = os.path.join(folder, base + ".mp4")
    n = 2
    while os.path.exists(p):
        p = os.path.join(folder, "%s (%d).mp4" % (base, n))
        n += 1
    return p


def _can_encode(name):
    try:
        av.codec.Codec(name, "w")
        return True
    except Exception:
        return False


def _encoder_candidates(src_codec: str, want: str, ten_bit: bool):
    hevc = want == "auto" and src_codec in ("hevc", "h265")
    cands = []
    if hevc:
        if sys.platform == "darwin":
            cands.append(("hevc_videotoolbox", "p010le" if ten_bit else "nv12", True))
        cands.append(("libx265", "yuv420p10le" if ten_bit else "yuv420p", False))
    if sys.platform == "darwin":
        cands.append(("h264_videotoolbox", "nv12", True))
    cands += [("libx264", "yuv420p", False), ("libopenh264", "yuv420p", False), ("mpeg4", "yuv420p", False)]
    return [c for c in cands if _can_encode(c[0])]


def _pick_encoder(dst, vs, src_cc, cfg):
    """Open the output container with the first encoder that actually works here."""
    ten_bit = "10" in (src_cc.format.name if src_cc.format else "")
    errors = []
    for cand in _encoder_candidates(src_cc.name, cfg["output"].get("codec", "auto"), ten_bit):
        out = av.open(dst, "w", options={"movflags": "+faststart"})
        try:
            st, pix = _open_single(out, vs, src_cc, cand)
            return out, st, pix
        except Exception as e:
            errors.append("%s: %s" % (cand[0], e))
            out.close()
            try:
                os.remove(dst)
            except OSError:
                pass
    raise RuntimeError("No usable video encoder found. " + "; ".join(errors))


def _open_single(out, vs, src_cc, cand):
    name, pix, hw = cand
    w, h = src_cc.width, src_cc.height
    rate = vs.average_rate or Fraction(30)
    bitrate = vs.bit_rate or src_cc.bit_rate or int(w * h * float(rate) * 0.15)
    st = out.add_stream(name, rate=rate)
    st.width, st.height, st.pix_fmt = w, h, pix
    st.time_base = vs.time_base
    cc = st.codec_context
    if name == "libx265":
        cc.options = {"crf": "20", "preset": "fast", "x265-params": "log-level=error"}
    elif name == "libx264":
        cc.options = {"crf": "18", "preset": "fast"}
    else:
        cc.bit_rate = int(bitrate)
    if "hevc" in name or "265" in name:
        st.codec_tag = "hvc1"
    for attr in ("color_primaries", "color_trc", "colorspace", "color_range"):
        try:
            setattr(cc, attr, getattr(src_cc, attr))
        except Exception:
            pass
    cc.open()
    return st, pix


def _rgba_frame(img: Image.Image, pts, tb) -> VideoFrame:
    f = VideoFrame.from_bytes(img.tobytes(), img.width, img.height, format="rgba")
    f.pts, f.time_base = pts, tb
    return f


def render_video(src: str, dst: Optional[str] = None, cfg: Optional[dict] = None,
                 progress: Optional[Callable[[str, float], None]] = None,
                 cancelled: Optional[Callable[[], bool]] = None) -> str:
    """Render `src` with the overlay into `dst` (default: next to src). Returns dst.
    progress(stage, fraction) is called with stage in {"telemetry", "map", "render"}."""
    cfg = cfg or settings_mod.load()
    progress = progress or (lambda s, f: None)
    cancelled = cancelled or (lambda: False)
    dst = dst or output_path(src, cfg)
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)

    progress("telemetry", 0.0)
    track = load_track(src)

    inp = av.open(src)
    try:
        vs = main_video_stream(inp)
        vs.thread_type = "AUTO"
        src_cc = vs.codec_context
        W, H = src_cc.width, src_cc.height
        ov = Overlay(track, cfg, W, H)
        progress("map", 0.0)
        ov.prefetch(lambda f: progress("map", f), cancelled)
        if cancelled():
            raise Cancelled()

        out, ost, out_pix = _pick_encoder(dst, vs, src_cc, cfg)
        audio_in = next((a for a in inp.streams.audio), None)
        audio_out = None
        if audio_in is not None:
            try:
                audio_out = out.add_stream_from_template(audio_in)
            except Exception:
                audio_out = None
        try:
            graph = av.filter.Graph()
            main_src = graph.add_buffer(template=vs)
            srcs = []
            last = main_src
            for i, (_, w, x, y) in enumerate(ov.widgets):
                probe_img = w.render(0.0)
                b = graph.add_buffer(width=probe_img.width, height=probe_img.height,
                                     format="rgba", time_base=vs.time_base)
                o = graph.add("overlay", "x=%d:y=%d:format=auto:eof_action=repeat" % (x, y))
                last.link_to(o, 0, 0)
                b.link_to(o, 0, 1)
                srcs.append((b, w))
                last = o
            fmt = graph.add("format", out_pix)
            sink = graph.add("buffersink")
            last.link_to(fmt)
            fmt.link_to(sink)
            graph.configure()

            total = vs.frames
            if not total and vs.duration:
                total = int(float(vs.duration * vs.time_base) * float(vs.average_rate or 30))
            start_pts = vs.start_time or 0
            n = 0
            t0 = time.time()

            def drain():
                while True:
                    try:
                        f = sink.pull()
                    except (av.BlockingIOError, av.EOFError):
                        return
                    f.pict_type = av.video.frame.PictureType.NONE
                    for p in ost.encode(f):
                        out.mux(p)

            streams = [vs] + ([audio_in] if audio_out is not None else [])
            for packet in inp.demux(*streams):
                if cancelled():
                    raise Cancelled()
                if packet.stream is audio_in:
                    if packet.dts is None:
                        continue
                    packet.stream = audio_out
                    out.mux(packet)
                    continue
                for frame in packet.decode():
                    if frame.pts is None:
                        continue
                    t = float((frame.pts - start_pts) * frame.time_base)
                    main_src.push(frame)
                    for b, w in srcs:
                        b.push(_rgba_frame(w.render(t), frame.pts, frame.time_base))
                    drain()
                    n += 1
                    if total:
                        progress("render", min(1.0, n / total))
            main_src.push(None)
            for b, _ in srcs:
                b.push(None)
            drain()
            for p in ost.encode(None):
                out.mux(p)
        finally:
            out.close()
    except BaseException:
        inp.close()
        try:
            if os.path.exists(dst):
                os.remove(dst)
        except OSError:
            pass
        raise
    inp.close()
    progress("render", 1.0)
    return dst


def video_info(src: str):
    """(width, height, duration_seconds) of the main video stream."""
    with av.open(src) as c:
        vs = main_video_stream(c)
        dur = float(vs.duration * vs.time_base) if vs.duration else float(c.duration or 0) / av.time_base
        return vs.codec_context.width, vs.codec_context.height, dur


def grab_frame(src: str, t: float) -> Image.Image:
    """Decode the frame closest to (at or after) t seconds."""
    with av.open(src) as c:
        vs = main_video_stream(c)
        vs.thread_type = "AUTO"
        start = vs.start_time or 0
        c.seek(max(0, int(t / vs.time_base)) + start, stream=vs, backward=True)
        frame = None
        for fr in c.decode(vs):
            frame = fr
            if fr.pts is not None and float((fr.pts - start) * fr.time_base) >= t - 0.02:
                break
        if frame is None:
            cc = vs.codec_context
            return Image.new("RGB", (cc.width, cc.height), (60, 70, 80))
        return frame.to_image()


def render_preview(src: Optional[str], cfg: dict, t: Optional[float] = None,
                   max_size: Optional[int] = None, offline: bool = False):
    """Return (PIL image of one frame with overlay, track). If src is None a neutral
    background and demo flight are used."""
    if src:
        track = load_track(src)
        t = track.duration / 2 if t is None else t
        bg = grab_frame(src, t)
        W, H = bg.size
    else:
        W, H = 3840, 2160
        bg = demo_background(W, H)
        track = demo_track()
        t = track.duration * 0.6 if t is None else t
        offline = True
    ov = Overlay(track, cfg, W, H, offline=offline)
    img = ov.compose(bg, t)
    if max_size and max(img.size) > max_size:
        img.thumbnail((max_size, max_size), Image.LANCZOS)
    return img, track


def demo_track():
    from .telemetry import Sample, Track
    import math
    s = []
    for i in range(0, 301):
        t = i * 0.1
        s.append(Sample(t, 45.0 + 0.0009 * math.sin(t / 9), 7.0 + t * 0.00012, 80 + t, 10.0, None))
    return Track(s, "demo")


def demo_background(W, H):
    img = Image.new("RGB", (W, H))
    top, bot = (92, 118, 150), (44, 52, 40)
    px = [tuple(int(top[c] + (bot[c] - top[c]) * (y / H)) for c in range(3)) for y in range(H)]
    col = Image.new("RGB", (1, H))
    col.putdata(px)
    return col.resize((W, H))
