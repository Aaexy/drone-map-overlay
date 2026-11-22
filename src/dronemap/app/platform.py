"""Small OS integrations: reveal in Finder/Explorer, open with the default app, assets."""

from __future__ import annotations

import os
import subprocess
import sys


def reveal(path: str) -> None:
    """Show `path` selected in the file manager (or open its folder)."""
    if not path:
        return
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path] if os.path.exists(path) else ["open", os.path.dirname(path)])
        elif sys.platform == "win32":
            if os.path.exists(path):
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            else:
                os.startfile(os.path.dirname(path))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path) or "."])
    except OSError:
        pass


def open_file(path: str) -> None:
    """Open `path` with its default application."""
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", path])
        elif sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", path])
    except OSError:
        pass


def asset(name: str) -> str:
    """Path of a bundled asset, both from source and inside the packaged app."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for base in (getattr(sys, "_MEIPASS", ""), here):
        if not base:
            continue
        for p in (os.path.join(base, "assets", name), os.path.join(base, "dronemap", "assets", name)):
            if os.path.exists(p):
                return p
    return ""
