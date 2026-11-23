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
import tempfile
import threading
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

Progress = Callable[[str, float], None]  # (stage, fraction): telemetry, map, render, finishing, saving
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


def _staging_dir(dest_folder: str, needed: int) -> Optional[str]:
    """A local folder to render into when the destination is on another drive (e.g. the
    SD card the video came from), or None to write straight to the destination."""
    local = tempfile.gettempdir()
    try:
        if os.stat(local).st_dev == os.stat(dest_folder).st_dev:
            return None
        if shutil.disk_usage(local).free < needed * 1.2:
            return None
    except OSError:
        return None
    return local


class _FollowCopy(threading.Thread):
    """Copy a file to `dst` while it's still being written, in large sequential chunks.

    The MP4 writer only ever appends, except for patching the box sizes in the first
    few bytes when it closes, so after `finish()` the start of the file is copied again.
    """

    CHUNK = 16 * 1024 * 1024
    HEAD = 1024 * 1024

    def __init__(self, src: str, dst: str):
        super().__init__(daemon=True, name="copy-to-destination")
        self.src, self.dst = src, dst
        self.writer_done = threading.Event()
        self.abort = threading.Event()
        self.copied = 0
        self.error: Optional[BaseException] = None

    def run(self) -> None:
        try:
            # The writer creates the file when it writes the first frame.
            while not os.path.exists(self.src) and not self.abort.is_set() and not self.writer_done.is_set():
                time.sleep(0.05)
            if self.abort.is_set():
                return
            with open(self.src, "rb") as fi, open(self.dst, "wb") as fo:
                while not self.abort.is_set():
                    chunk = fi.read(self.CHUNK)
                    if chunk:
                        fo.write(chunk)
                        self.copied += len(chunk)
                    elif self.writer_done.is_set():
                        break  # writer closed and we've reached the end
                    else:
                        time.sleep(0.2)
        except BaseException as e:  # reported by finish()
            self.error = e

    def finish(self, progress: Callable[[float], None], cancelled: CancelCheck) -> None:
        """Call after the writer has closed the file: wait for the tail, then fix the head."""
        self.writer_done.set()
        total = max(1, os.path.getsize(self.src))
        while self.is_alive():
            if cancelled():
                self.abort.set()
                self.join()
                raise Cancelled()
            progress(min(1.0, self.copied / total))
            self.join(0.1)
        if self.error is not None:
            raise self.error
        with open(self.src, "rb") as fi, open(self.dst, "r+b") as fo:
            fo.write(fi.read(self.HEAD))
            fo.flush()
            os.fsync(fo.fileno())
        if os.path.getsize(self.dst) != os.path.getsize(self.src):
            raise OSError("copy to the destination folder is incomplete")
        shutil.copystat(self.src, self.dst)
        progress(1.0)

    def stop(self) -> None:
        self.abort.set()
        self.writer_done.set()
        if self.is_alive():
            self.join()


# ------------------------------------------------------------------ rendering
def _rgba_frame(img: Image.Image, pts, tb) -> VideoFrame:
    f = VideoFrame.from_bytes(img.tobytes(), img.width, img.height, format="rgba")
    f.pts, f.time_base = pts, tb
    return f


# Full-frame layers are converted to the video's own pixel format (with alpha) on the worker
# threads; left to the filter graph, that conversion runs on one core and slows 4K down.
_ALPHA_FORMATS = {"yuv420p": "yuva420p", "yuvj420p": "yuva420p", "yuv420p10le": "yuva420p10le"}


def _widget_frame(widget, t: float, fmt: Optional[str]) -> VideoFrame:
    if fmt and hasattr(widget, "render_frame"):
        return widget.render_frame(t, fmt)
    img = widget.render(t)
    f = VideoFrame.from_bytes(img.tobytes(), img.width, img.height, format="rgba")
    return f.reformat(format=fmt) if fmt else f


def _rotation_filters(rot: int) -> list[tuple[str, str]]:
    return {90: [("transpose", "clock")], 180: [("hflip", ""), ("vflip", "")], 270: [("transpose", "cclock")]}.get(rot, [])


def render_video(
    src: str,
    dst: Optional[str] = None,
    cfg: Optional[dict] = None,
    progress: Optional[Progress] = None,
    cancelled: Optional[CancelCheck] = None,
    workers: Optional[int] = None,
    limit: Optional[float] = None,
) -> RenderResult:
    """Render `src` with the overlay into `dst` (default: next to `src`).
    `limit` stops after that many seconds of video (quick test renders, benchmarks)."""
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
    final_tmp = os.path.join(folder, ".%s.part" % os.path.basename(dst))
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

    ov = Overlay(track, cfg, info.width, info.height, video=src)
    progress("map", 0.0)
    ov.prefetch(lambda f: progress("map", f), cancelled, lambda f: progress("streets", f), lambda f: progress("align", f))
    if cancelled():
        raise Cancelled()

    warnings: list[str] = []
    inp = av.open(src)
    out = None
    copier: Optional[_FollowCopy] = None
    try:
        vs = main_video_stream(inp)
        vs.thread_type = "AUTO"
        seconds = min(info.duration, limit) if limit is not None else info.duration
        needed = int((vs.bit_rate or 50_000_000) * max(1.0, seconds) / 8 * 1.1) + 100_000_000
        _check_space(folder, needed)
        # Writing to a memory card while reading from it roughly halves the speed: render to
        # the internal disk and copy the finished file across in one go.
        stage_dir = _staging_dir(folder, needed)
        tmp = os.path.join(stage_dir, "dronemap-%d-%s.part" % (os.getpid(), os.path.basename(dst))) if stage_dir else final_tmp
        log.info("Writing to %s", "local staging file " + tmp if stage_dir else tmp)
        out, ost, cand = open_output(tmp, vs, cfg["output"].get("codec", "auto"), info.width, info.height)
        if stage_dir:  # stream the file to its destination while it's being rendered
            copier = _FollowCopy(tmp, final_tmp)
            copier.start()
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
        main_fmt = vs.codec_context.pix_fmt or ""
        for p in ov.widgets:
            probe_img = p.widget.render(0.0)
            full = p.name == "streets" and probe_img.width % 2 == 0 and probe_img.height % 2 == 0
            fmt = _ALPHA_FORMATS.get(main_fmt) if full else None
            buf = graph.add_buffer(width=probe_img.width, height=probe_img.height, format=fmt or "rgba", time_base=vs.time_base)
            node = graph.add("overlay", "x=%d:y=%d:format=auto:eof_action=repeat" % (p.x, p.y))
            last.link_to(node, 0, 0)
            buf.link_to(node, 0, 1)
            sources.append((buf, p.widget, fmt))
            last = node
        fmt = graph.add("format", ost.pix_fmt)
        sink = graph.add("buffersink")
        last.link_to(fmt)
        fmt.link_to(sink)
        graph.configure()

        total = vs.frames or int(info.duration * float(nominal_rate(vs))) or 1
        if limit is not None:
            total = min(total, int(limit * float(nominal_rate(vs))) + 1)
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
            pending.append((frame, [pool.submit(_widget_frame, w, t, fmt) for _, w, fmt in sources]))

        def flush_one() -> None:
            frame, futures = pending.popleft()
            main_src.push(frame)
            for (buf, _, _), fut in zip(sources, futures):
                f = fut.result()
                f.pts, f.time_base = frame.pts, frame.time_base
                buf.push(f)
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
                if limit is not None and packet.pts is not None and float(packet.pts * packet.time_base) > limit + 1:
                    break
                if packet.stream is audio_in:
                    if packet.dts is not None:
                        packet.stream = audio_out
                        out.mux(packet)
                    continue
                for frame in packet.decode():
                    if frame.pts is None or (limit is not None and float((frame.pts - start_pts) * frame.time_base) > limit):
                        continue
                    submit(frame, pool)
                    while len(pending) > n_workers + 2:
                        flush_one()
            while pending:
                flush_one()
        progress("finishing", 0.0)
        main_src.push(None)
        for buf, _, _ in sources:
            buf.push(None)
        drain()
        for pkt in ost.encode(None):
            out.mux(pkt)
        t_close = time.time()
        out.close()
        log.info("Finalising the file took %.1fs", time.time() - t_close)
        out = None
        inp.close()
        if copier is not None:
            copier.finish(lambda f: progress("saving", f), cancelled)
            copier = None
            os.remove(tmp)
        os.replace(final_tmp, dst)
    except BaseException:
        if copier is not None:
            copier.stop()
        for c in (out, inp):
            try:
                if c is not None:
                    c.close()
            except Exception:
                pass
        for leftover in {locals().get("tmp"), final_tmp}:
            try:
                if leftover:
                    os.remove(leftover)
            except OSError:
                pass
        raise

    if ov.streets_error():
        warnings.append(ov.streets_error())
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
    ov = Overlay(track, cfg, bg.width, bg.height, offline=offline, video=src)
    if ov.streets is not None:
        ov.streets.prefetch(around=t)
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
