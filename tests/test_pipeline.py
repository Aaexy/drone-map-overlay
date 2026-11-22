import io
import os
import shutil
import subprocess

import av
import pytest
from conftest import offline_cfg

from dronemap.errors import Cancelled, UserFacingError, describe
from dronemap.pipeline import output_path, probe, render_preview, render_video


def frame_times(path):
    with av.open(path) as c:
        return [f.time for f in c.decode(c.streams.video[0])]


def test_render_end_to_end(sample_video, tmp_path):
    out = tmp_path / "out.mp4"
    stages = []
    res = render_video(sample_video, str(out), offline_cfg(), lambda s, f: stages.append(s))
    assert res.output == str(out) and res.frames == 60 and not res.warnings
    assert {"telemetry", "map", "render"} <= set(stages)
    with av.open(str(out)) as c:
        v = c.streams.video[0]
        assert (v.codec_context.width, v.codec_context.height) == (320, 240)
        assert len(c.streams.audio) == 1
    assert len(frame_times(str(out))) == 60
    assert not [f for f in os.listdir(tmp_path) if f.endswith(".part")]


def test_dropped_frames_keep_their_timestamps(tmp_path):
    """Drones sometimes drop frames, so the average frame rate becomes an odd fraction.
    Timestamps must be kept exactly (rounding to that rate once produced duplicate
    timestamps and an EINVAL from the MP4 muxer)."""
    import make_sample

    dropped = {20, 21, 22, 23, 24, 25, 26, 70, 71, 72, 73, 74, 75, 76, 77, 78, 79, 80}
    src = make_sample.make(str(tmp_path), seconds=6.0, fps=60, audio=False, drop=dropped, name="gappy")
    with av.open(src) as c:
        assert c.streams.video[0].average_rate != 60
    out = tmp_path / "out.mp4"
    render_video(src, str(out), offline_cfg())
    src_t, out_t = frame_times(src), frame_times(str(out))
    assert len(out_t) == len(src_t) == 360 - len(dropped)
    assert out_t == pytest.approx(src_t, abs=1e-3)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs the ffmpeg command to make rotated samples")
@pytest.mark.parametrize("ccw", [90, 180, 270])
def test_rotated_videos_come_out_upright(sample_video, tmp_path, ccw):
    """Compare with ffmpeg's own auto-rotation, which is what players show."""
    from PIL import Image

    src = tmp_path / ("rot%d.mp4" % ccw)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-display_rotation:v:0", str(ccw), "-i", sample_video, "-c", "copy", str(src)], check=True
    )
    shutil.copy(sample_video[:-4] + ".SRT", str(src)[:-4] + ".SRT")
    cfg = offline_cfg(map={"enabled": False}, stats={"enabled": False})
    res = render_video(str(src), str(tmp_path / "out.mp4"), cfg)

    def first_frame(p):
        png = subprocess.check_output(
            ["ffmpeg", "-v", "error", "-i", p, "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"]
        )
        return Image.open(io.BytesIO(png)).convert("RGB")

    want, got = first_frame(str(src)), first_frame(res.output)
    assert want.size == got.size == (probe(str(src)).width, probe(str(src)).height)

    def yellow_corner(im):
        w, h = im.size
        corners = {"tl": (0.1, 0.1), "tr": (0.9, 0.1), "bl": (0.1, 0.9), "br": (0.9, 0.9)}
        return [k for k, (fx, fy) in corners.items() if im.getpixel((int(w * fx), int(h * fy)))[2] < 80]

    assert yellow_corner(got) == yellow_corner(want) and len(yellow_corner(want)) == 1


def test_cancel_leaves_nothing_behind(sample_video, tmp_path):
    out = tmp_path / "out.mp4"
    calls = {"n": 0}

    def cancelled():
        calls["n"] += 1
        return calls["n"] > 5  # cancel shortly after rendering starts

    with pytest.raises(Cancelled):
        render_video(sample_video, str(out), offline_cfg(), cancelled=cancelled)
    assert os.listdir(tmp_path) == []


def test_refuses_to_overwrite_the_source(sample_video):
    with pytest.raises(UserFacingError):
        render_video(sample_video, sample_video, offline_cfg())


def test_output_path_never_overwrites(tmp_path):
    src = tmp_path / "clip.MP4"
    src.write_bytes(b"")
    cfg = offline_cfg()
    first = output_path(str(src), cfg)
    assert first.endswith("clip_map.mp4")
    open(first, "w").close()
    assert output_path(str(src), cfg).endswith("clip_map (2).mp4")


def test_unreadable_file_has_a_helpful_message(tmp_path):
    bad = tmp_path / "broken.mp4"
    bad.write_bytes(b"not a video at all" * 100)
    (tmp_path / "broken.SRT").write_text("")
    with pytest.raises(Exception) as ei:
        render_video(str(bad), str(tmp_path / "o.mp4"), offline_cfg())
    msg, hint = describe(ei.value)
    assert msg == "This file couldn't be read as a video."
    assert hint and "damaged" in hint


def test_preview(sample_video):
    img, track = render_preview(sample_video, offline_cfg(), t=1.0)
    assert img.size == (320, 240) and track.source == "SRT captions"
