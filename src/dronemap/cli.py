"""Command-line interface: `dronemap VIDEO [VIDEO ...]` (no arguments opens the app)."""
from __future__ import annotations

import argparse
import sys
import time

from . import __version__
from . import settings as settings_mod


def main(argv=None):
    ap = argparse.ArgumentParser(prog="dronemap", description="Burn a live GPS map and flight stats into drone videos.")
    ap.add_argument("videos", nargs="*", help="video files (none = open the app)")
    ap.add_argument("-o", "--output", help="output file (only with a single video)")
    ap.add_argument("--settings", help="settings JSON file (default: the app's saved settings)")
    ap.add_argument("--preview", type=float, metavar="SECONDS", help="write a PNG of one frame instead of a video")
    ap.add_argument("--version", action="version", version=__version__)
    a = ap.parse_args(argv)

    if not a.videos:
        from .gui import run
        return run()

    cfg = settings_mod.load()
    if a.settings:
        import json
        with open(a.settings, encoding="utf-8") as f:
            cfg = settings_mod._merge(settings_mod.DEFAULTS, json.load(f))

    if a.preview is not None:
        from .encode import render_preview
        for v in a.videos:
            img, _ = render_preview(v, cfg, t=a.preview)
            out = a.output or v.rsplit(".", 1)[0] + "_preview.png"
            img.convert("RGB").save(out)
            print(out)
        return 0

    from .encode import render_video
    if a.output and len(a.videos) > 1:
        ap.error("--output only works with one video")
    rc = 0
    for v in a.videos:
        t0 = time.time()
        last = [""]

        def prog(stage, f):
            line = "\r%-9s %5.1f%%" % (stage, f * 100)
            if line != last[0]:
                sys.stderr.write(line)
                last[0] = line

        try:
            out = render_video(v, a.output, cfg, prog)
            sys.stderr.write("\r")
            print("%s  (%.0fs)" % (out, time.time() - t0))
        except Exception as e:
            sys.stderr.write("\r")
            print("%s: %s" % (v, e), file=sys.stderr)
            rc = 1
    return rc
