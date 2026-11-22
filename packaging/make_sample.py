"""Write a tiny test video + DJI-style .SRT + offline settings. Used by tests and CI smoke tests.

python packaging/make_sample.py OUT_DIR
"""

import json
import math
import os
import sys
from fractions import Fraction

import av
from av.audio.frame import AudioFrame
from av.video.frame import VideoFrame


def make(out_dir, seconds=2.0, fps=30, size=(320, 240), audio=True, drop=(), name="sample"):
    """drop: frame numbers to leave out, like a drone that dropped frames while recording."""
    os.makedirs(out_dir, exist_ok=True)
    video = os.path.join(out_dir, name + ".mp4")
    W, H = size
    n = int(seconds * fps)
    with av.open(video, "w") as c:
        vs = c.add_stream("libx264" if _has("libx264") else "mpeg4", rate=fps)
        vs.width, vs.height, vs.pix_fmt = W, H, "yuv420p"
        a_st = c.add_stream("aac", rate=48000) if audio else None
        for i in range(n):
            if i in drop:
                continue
            g = int(255 * i / max(1, n - 1))
            # solid background with a yellow block in the top-left corner (to check orientation)
            row_bg = bytes([60, g, 160, 255]) * W
            row_mark = bytes([255, 220, 0, 255]) * (W // 4) + bytes([60, g, 160, 255]) * (W - W // 4)
            data = row_mark * (H // 4) + row_bg * (H - H // 4)
            f = VideoFrame.from_bytes(data, W, H, format="rgba")
            f.pts = i
            f.time_base = Fraction(1, fps)
            for p in vs.encode(f):
                c.mux(p)
        for p in vs.encode(None):
            c.mux(p)
        if a_st is not None:
            spf = 1024
            total = int(seconds * 48000)
            for k in range(0, total, spf):
                samples = bytearray()
                for j in range(spf):
                    v = int(3000 * math.sin(2 * math.pi * 440 * (k + j) / 48000))
                    samples += v.to_bytes(2, "little", signed=True)
                fr = AudioFrame(format="s16", layout="mono", samples=spf)
                fr.planes[0].update(bytes(samples))
                fr.sample_rate = 48000
                fr.pts = k
                fr.time_base = Fraction(1, 48000)
                for p in a_st.encode(fr):
                    c.mux(p)
            for p in a_st.encode(None):
                c.mux(p)
    # DJI-style captions: one cue per 1/10 s, flying east-north-east at ~10 m/s
    lines = []
    cues = int(seconds * 10)
    for i in range(cues):
        t0, t1 = i / 10, (i + 1) / 10
        lat = 46.5000 + i * 0.0000035
        lon = 7.5000 + i * 0.0000130
        lines.append(
            '%d\n%s --> %s\n<font size="28">FrameCnt: %d, DiffTime: 100ms\n'
            "[iso: 100] [shutter: 1/500.0] [fnum: 1.8] [ev: 0] [color_md: default] "
            "[latitude: %.6f] [longitude: %.6f] [rel_alt: %.3f abs_alt: %.3f] </font>\n"
            % (i + 1, _ts(t0), _ts(t1), i * 3 + 1, lat, lon, 50 + i * 0.5, 450 + i * 0.5)
        )
    with open(os.path.join(out_dir, name + ".SRT"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    with open(os.path.join(out_dir, "settings.json"), "w", encoding="utf-8") as f:
        json.dump({"tiles": {"url": "placeholder"}, "stats": {"distance": True}}, f)
    return video


def _ts(t):
    ms = int(round(t * 1000))
    return "%02d:%02d:%02d,%03d" % (ms // 3600000, ms // 60000 % 60, ms // 1000 % 60, ms % 1000)


def _has(name):
    try:
        av.codec.Codec(name, "w")
        return True
    except Exception:
        return False


if __name__ == "__main__":
    print(make(sys.argv[1] if len(sys.argv) > 1 else "sample"))
