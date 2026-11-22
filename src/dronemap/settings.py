"""User settings: what the overlay shows, how it looks, and where it sits.

Widget boxes are stored as fractions of the video frame (x, y, w of the frame
width; y, h of the frame height), so one layout works for any resolution.
"""
from __future__ import annotations

import copy
import json
import os
import sys

APP_NAME = "DroneMapOverlay"

DEFAULTS = {
    "map": {
        "enabled": True,
        "box": [0.025, 0.566, 0.16, 0.287],
        "zoom": 17,
        "orientation": "north",      # "north" (north up) or "heading" (direction of flight up)
        "style": "standard",         # "standard", "grayscale", "dark"
        "show_path": True,
        "show_full_route": False,
        "show_cone": True,
        "show_north": True,
        "show_credit": False,
    },
    "stats": {
        "enabled": True,
        "box": [0.025, 0.865, 0.16, 0.1],
        "speed": True,
        "altitude": True,
        "heading": True,
        "distance": False,
        "coords": True,
    },
    "style": {
        "accent": "#ff5c36",
        "panel_opacity": 0.8,
        "roundness": 0.06,
        "units": "metric",           # "metric" or "imperial"
    },
    "output": {
        "codec": "auto",             # "auto" (match source, HEVC/H.264) or "h264" (most compatible)
        "folder": "",                # "" = next to the source video
        "suffix": "_map",
    },
    "app": {
        "last_video": "",            # used for the settings preview
        "theme": "dark",             # "dark", "light" or "system"
    },
    "tiles": {
        "url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "credit": "© OpenStreetMap contributors",
    },
}


def config_dir() -> str:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, APP_NAME)


def cache_dir() -> str:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, APP_NAME, "cache")
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~/Library/Caches"), APP_NAME)
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, APP_NAME)


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _merge(out[k], v)
        elif k in out:
            out[k] = v
    return out


def defaults() -> dict:
    return copy.deepcopy(DEFAULTS)


def settings_path() -> str:
    return os.path.join(config_dir(), "settings.json")


def load() -> dict:
    try:
        with open(settings_path(), encoding="utf-8") as f:
            return _merge(DEFAULTS, json.load(f))
    except (OSError, ValueError):
        return defaults()


def save(s: dict) -> None:
    os.makedirs(config_dir(), exist_ok=True)
    tmp = settings_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)
    os.replace(tmp, settings_path())
