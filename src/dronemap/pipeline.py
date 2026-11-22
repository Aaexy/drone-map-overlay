"""Decode a video, burn the overlay in, and encode the result.

Decoding, compositing and encoding run inside FFmpeg (via PyAV). Widget images are
drawn on a small thread pool a few frames ahead of the encoder, so drawing overlaps
with decoding and encoding instead of adding to them.
"""

from __future__ import annotations

import logging
import math
import os
import shutil
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Optional

import av
from av.video.frame import VideoFrame
from PIL import Image

from . import settings as settings_mod
from .encoders import nominal_rate, open_output
from .errors import Cancelled, UserFacingError
from .overlay import Overlay
from .telemetry import Sample, Track, load_track

log = logging.getLogger(__name__)

Progress = Callable[[str, float], None]  # (stage, fraction); stage in telemetry/map/render
CancelCheck = Callable[[], bool]


@dataclass
class VideoInfo:
    width: int  # as displayed (rotation applied)
    height: int
    duration: float  # seconds
    rotation: int  # clockwise degrees needed to display the stored frames upright


@dataclass
class RenderResult:
    output: str
    frames: int
    seconds: float
    encoder: str
    warnings: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ probing
def main_video_stream(container):
    vids = [s for s in container.streams.video if not (s.disposition & av.stream.Disposition.attached_pic)]
    if not vids:
        raise UserFacingError("This file has no video in it.")
    return max(vids, key=lambda s: (s.codec_context.width or 0) * (s.codec_context.height or 0))


def _frame_rotation(frame) -> int:
    """Clockwise degrees (0/90/180/270) to turn a decoded frame upright. FFmpeg reports
    the display matrix as a counter-clockwise angle."""
    try:
        ccw = float(getattr(frame, "rotation", 0) or 0)
    except (TypeError, ValueError):
        return 0
    return int(round(-ccw / 90.0)) * 90 % 360


def probe(src: str) -> VideoInfo:
    with av.open(src) as c:
        vs = main_video_stream(c)
        w, h = vs.codec_context.width, vs.codec_context.height
        dur = float(vs.duration * vs.time_base) if vs.duration else float(c.duration or 0) / av.time_base
        rot = 0
        for frame in c.decode(vs):
            rot = _frame_rotation(frame)
            break
    if rot in (90, 270):
        w, h = h, w
    return VideoInfo(w, h, dur, rot)


def _upright(img: Image.Image, rot: int) -> Image.Image:
    return img.rotate(-rot, expand=True) if rot else img


def grab_frame(src: str, t: float) -> Image.Image:
    """The frame at (or just after) t seconds, upright."""
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
        return _upright(frame.to_image(), _frame_rotation(frame))


# ------------------------------------------------------------------ paths
def output_path(src: str, cfg: dict) -> str:
    folder = cfg["output"].get("folder") or os.path.dirname(os.path.abspath(src))
    base = os.path.splitext(os.path.basename(src))[0] + (cfg["output"].get("suffix") or "_map")
    p = os.path.join(folder, base + ".mp4")
    n = 2
    while os.path.exists(p):
        p = os.path.join(folder, "%s (%d).mp4" % (base, n))
        n += 1
    return p


def _check_space(folder: str, needed: int) -> None:
    try:
        free = shutil.disk_usage(folder).free
    except OSError:
        return
    if free < needed:
        gb = lambda b: "%.1f GB" % (b / 1e9)
        raise UserFacingError(
            "Not enough free space for the new video (needs about %s, %s free)." % (gb(needed), gb(free)),
            "Free up space or choose another output folder in Settings → Output.",
        )


# ------------------------------------------------------------------ rendering
def _rgba_frame(img: Image.Image, pts, tb) -> VideoFrame:
    f = VideoFrame.from_bytes(img.tobytes(), img.width, img.height, format="rgba")
    f.pts, f.time_base = pts, tb
    return f


def _rotation_filters(rot: int) -> list[tuple[str, str]]:
    return {90: [("transpose", "clock")], 180: [("hflip", ""), ("vflip", "")], 270: [("transpose", "cclock")]}.get(rot, [])


def render_video(
    src: str,
    dst: Optional[str] = None,
    cfg: Optional[dict] = None,
    progress: Optional[Progress] = None,
    cancelled: Optional[CancelCheck] = None,
    workers: Optional[int] = None,
) -> RenderResult:
    """Render `src` with the overlay into `dst` (default: next to `src`)."""
    cfg = settings_mod.normalize(cfg) if cfg is not None else settings_mod.load()
    progress = progress or (lambda s, f: None)
    cancelled = cancelled or (lambda: False)
    dst = os.path.abspath(dst or output_path(src, cfg))
    if os.path.abspath(src) == dst:
        raise UserFacingError(
            "The output file would replace the original video.", "Change the file name ending in Settings → Output."
        )
    folder = os.path.dirname(dst)
    os.makedirs(folder, exist_ok=True)
    tmp = os.path.join(folder, ".%s.part" % os.path.basename(dst))
    t_start = time.time()
    log.info("Render %s -> %s", src, dst)

    progress("telemetry", 0.0)
    track = load_track(src)
    info = probe(src)
    log.info(
        "Video %dx%d, %.1fs, rotation %d; GPS: %d samples from %s",
        info.width,
        info.height,
        info.duration,
        info.rotation,
        len(track.samples),
        track.source,
    )

    ov = Overlay(track, cfg, info.width, info.height)
    progress("map", 0.0)
    ov.prefetch(lambda f: progress("map", f), cancelled)
    if cancelled():
        raise Cancelled()

    warnings: list[str] = []
    inp = av.open(src)
    out = None
    try:
        vs = main_video_stream(inp)
        vs.thread_type = "AUTO"
        _check_space(folder, int((vs.bit_rate or 50_000_000) * max(1.0, info.duration) / 8 * 1.1) + 100_000_000)
        out, ost, cand = open_output(tmp, vs, cfg["output"].get("codec", "auto"), info.width, info.height)
        created = inp.metadata.get("creation_time")
        if created:
            out.metadata["creation_time"] = created
        audio_in = inp.streams.audio[0] if inp.streams.audio else None
        audio_out = None
        if audio_in is not None:
            try:
                audio_out = out.add_stream_from_template(audio_in)
            except Exception as e:
                warnings.append("The sound couldn't be copied (%s)." % e)
                log.warning("Audio not copied: %s", e)

        # Filter graph: [video] -> rotate upright -> overlay widget 1 -> widget 2 -> encoder format
        graph = av.filter.Graph()
        main_src = graph.add_buffer(template=vs)
        last = main_src
        for name, args in _rotation_filters(info.rotation):
            node = graph.add(name, args) if args else graph.add(name)
            last.link_to(node)
            last = node
        sources = []
        for p in ov.widgets:
            probe_img = p.widget.render(0.0)
            buf = graph.add_buffer(width=probe_img.width, height=probe_img.height, format="rgba", time_base=vs.time_base)
            node = graph.add("overlay", "x=%d:y=%d:format=auto:eof_action=repeat" % (p.x, p.y))
            last.link_to(node, 0, 0)
            buf.link_to(node, 0, 1)
            sources.append((buf, p.widget))
            last = node
        fmt = graph.add("format", ost.pix_fmt)
        sink = graph.add("buffersink")
        last.link_to(fmt)
        fmt.link_to(sink)
        graph.configure()

        total = vs.frames or int(info.duration * float(nominal_rate(vs))) or 1
        start_pts = vs.start_time or 0
        state = {"frames": 0, "last_pts": None, "last_report": 0.0}

        def drain() -> None:
            while True:
                try:
                    f = sink.pull()
                except (av.BlockingIOError, av.EOFError):
                    return
                # Never hand the encoder a timestamp that doesn't move forward.
                if f.pts is not None and state["last_pts"] is not None and f.pts <= state["last_pts"]:
                    continue
                state["last_pts"] = f.pts
                f.pict_type = av.video.frame.PictureType.NONE
                for pkt in ost.encode(f):
                    out.mux(pkt)

        n_workers = workers or min(8, max(2, (os.cpu_count() or 4) - 2))
        pending: deque = deque()

        def submit(frame, pool) -> None:
            t = float((frame.pts - start_pts) * frame.time_base)
            pending.append((frame, [pool.submit(w.render, t) for _, w in sources]))

        def flush_one() -> None:
            frame, futures = pending.popleft()
            main_src.push(frame)
            for (buf, _), fut in zip(sources, futures):
                buf.push(_rgba_frame(fut.result(), frame.pts, frame.time_base))
            drain()
            state["frames"] += 1
            now = time.time()
            if now - state["last_report"] > 0.1:
                state["last_report"] = now
                progress("render", min(1.0, state["frames"] / total))

        with ThreadPoolExecutor(n_workers, thread_name_prefix="widget") as pool:
            streams = [vs] + ([audio_in] if audio_out is not None else [])
            for packet in inp.demux(*streams):
                if cancelled():
                    raise Cancelled()
                if packet.stream is audio_in:
                    if packet.dts is not None:
                        packet.stream = audio_out
                        out.mux(packet)
                    continue
                for frame in packet.decode():
                    if frame.pts is None:
                        continue
                    submit(frame, pool)
                    while len(pending) > n_workers + 2:
                        flush_one()
            while pending:
                flush_one()
        main_src.push(None)
        for buf, _ in sources:
            buf.push(None)
        drain()
        for pkt in ost.encode(None):
            out.mux(pkt)
        out.close()
        out = None
        inp.close()
        os.replace(tmp, dst)
    except BaseException:
        for c in (out, inp):
            try:
                if c is not None:
                    c.close()
            except Exception:
                pass
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise

    failed = ov.failed_tiles()
    if failed:
        warnings.append(
            "%d map tile%s couldn't be downloaded (no internet connection?), so parts of the map "
            "are blank." % (failed, "" if failed == 1 else "s")
        )
    secs = time.time() - t_start
    log.info("Done: %d frames in %.1fs (%.1f fps) with %s", state["frames"], secs, state["frames"] / max(secs, 1e-6), cand.codec)
    for w in warnings:
        log.warning(w)
    progress("render", 1.0)
    return RenderResult(dst, state["frames"], secs, cand.codec, warnings)


# ------------------------------------------------------------------ previews
def render_preview(
    src: Optional[str], cfg: dict, t: Optional[float] = None, max_size: Optional[int] = None, offline: bool = False
):
    """(PIL image of one frame with the overlay, track). Without `src`, a neutral
    background and a demo flight are used."""
    cfg = settings_mod.normalize(cfg)
    if src:
        track = load_track(src)
        t = track.duration / 2 if t is None else t
        bg = grab_frame(src, t)
    else:
        bg, track = demo_background(3840, 2160), demo_track()
        t = track.duration * 0.6 if t is None else t
        offline = True
    ov = Overlay(track, cfg, bg.width, bg.height, offline=offline)
    img = ov.compose(bg, t)
    if max_size and max(img.size) > max_size:
        img.thumbnail((max_size, max_size), Image.LANCZOS)
    return img, track


def demo_track() -> Track:
    """A gentle 30 s demo flight for the settings preview."""
    samples = [
        Sample(i * 0.1, 45.0 + 0.0009 * math.sin(i * 0.1 / 9), 7.0 + i * 0.1 * 0.00012, 80 + i * 0.1, 10.0) for i in range(301)
    ]
    return Track(samples, "demo")


def demo_background(W: int, H: int) -> Image.Image:
    top, bot = (92, 118, 150), (44, 52, 40)
    col = Image.new("RGB", (1, H))
    col.putdata([tuple(int(top[c] + (bot[c] - top[c]) * y / H) for c in range(3)) for y in range(H)])
    return col.resize((W, H))
