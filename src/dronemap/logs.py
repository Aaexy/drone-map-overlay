"""Logging to a small rotating file, so problems can be diagnosed after the fact."""

from __future__ import annotations

import logging
import logging.handlers
import os
import platform
import sys

from . import __version__
from .settings import log_dir

_configured = False


def log_file() -> str:
    return os.path.join(log_dir(), "dronemap.log")


def setup(verbose: bool = False, to_stderr: bool = False) -> None:
    global _configured
    if _configured:
        return
    _configured = True
    root = logging.getLogger("dronemap")
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    try:
        os.makedirs(log_dir(), exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(log_file(), maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError:
        pass
    if to_stderr:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        sh.setLevel(logging.DEBUG if verbose else logging.WARNING)
        root.addHandler(sh)
    try:
        import av

        av_info = "PyAV %s" % av.__version__
    except Exception:  # pragma: no cover
        av_info = "PyAV ?"
    root.info(
        "Drone Map Overlay %s on %s %s, Python %s, %s",
        __version__,
        platform.system(),
        platform.release(),
        platform.python_version(),
        av_info,
    )
