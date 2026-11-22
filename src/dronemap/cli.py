"""Command line: `dronemap VIDEO [VIDEO ...]`. With no videos, opens the app."""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Optional

from . import __version__, logs
from . import settings as settings_mod
from .errors import describe, log_failure

log = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="dronemap", description="Burn a live GPS map and flight stats into drone videos.")
    ap.add_argument("videos", nargs="*", help="video files (none: open the app)")
    ap.add_argument("-o", "--output", help="output file (only with a single video)")
    ap.add_argument("--settings", metavar="JSON", help="settings file to use instead of the app's saved settings")
    ap.add_argument("--preset", metavar="NAME", help="use a saved preset's look and layout (a preset name or a .json file)")
    ap.add_argument(
        "--streets",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="draw street lines and names into the picture (or not), whatever the settings say",
    )
    ap.add_argument("--list-presets", action="store_true", help="list the saved presets and exit")
    ap.add_argument("--preview", type=float, metavar="SECONDS", help="write a PNG of one frame instead of a video")
    ap.add_argument("--limit", type=float, metavar="SECONDS", help="only render the first SECONDS of each video")
    ap.add_argument("-v", "--verbose", action="store_true", help="print detailed progress information")
    ap.add_argument("--version", action="version", version="%(prog)s " + __version__)
    return ap


def main(argv: Optional[list[str]] = None) -> int:
    ap = build_parser()
    a = ap.parse_args(argv)
    if a.list_presets:
        for p in settings_mod.list_presets():
            print(p["name"] + ("  (built in)" if p["builtin"] else ""))
        return 0
    if not a.videos:
        from .app import run

        return run()
    if a.output and len(a.videos) > 1:
        ap.error("--output only works with one video")
    logs.setup(verbose=a.verbose, to_stderr=True)

    try:
        cfg = settings_mod.load_file(a.settings) if a.settings else settings_mod.load()
    except (OSError, ValueError) as e:
        ap.error("can't read settings file: %s" % e)
    if a.preset:
        try:
            cfg = settings_mod.apply_preset(cfg, settings_mod.load_preset(a.preset))
        except settings_mod.PresetError as e:
            names = ", ".join(p["name"] for p in settings_mod.list_presets())
            ap.error("%s Available presets: %s" % (e, names))
    if a.streets is not None:
        cfg["streets"]["enabled"] = a.streets

    if a.preview is not None:
        from .pipeline import render_preview

        for v in a.videos:
            try:
                img, _ = render_preview(v, cfg, t=a.preview)
            except Exception as e:
                return _fail(v, e)
            out = a.output or v.rsplit(".", 1)[0] + "_preview.png"
            img.convert("RGB").save(out)
            print(out)
        return 0

    from .pipeline import render_video

    rc = 0
    for v in a.videos:
        try:
            res = render_video(v, a.output, cfg, _progress_printer(), limit=a.limit)
        except KeyboardInterrupt:
            sys.stderr.write("\rCancelled.          \n")
            return 130
        except Exception as e:
            rc = _fail(v, e)
            continue
        sys.stderr.write("\r" + " " * 20 + "\r")
        print("%s  (%d frames in %.0fs, %s)" % (res.output, res.frames, res.seconds, res.encoder))
        for w in res.warnings:
            print("  warning: " + w, file=sys.stderr)
    return rc


def _progress_printer():
    last = [""]

    def prog(stage: str, f: float) -> None:
        line = "\r%-9s %5.1f%%" % (stage, f * 100)
        if line != last[0]:
            sys.stderr.write(line)
            sys.stderr.flush()
            last[0] = line

    return prog


def _fail(video: str, e: BaseException) -> int:
    sys.stderr.write("\r")
    msg, hint = describe(e)
    log_failure(log, "%s failed" % video, e)
    print("%s: %s" % (video, msg), file=sys.stderr)
    if hint:
        print("  " + hint, file=sys.stderr)
    if not hint or "log" in hint:
        print("  (%s: %s)" % (type(e).__name__, e), file=sys.stderr)
    return 1
