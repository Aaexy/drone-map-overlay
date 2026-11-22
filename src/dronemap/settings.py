"""User settings: what the overlay shows, how it looks, and where it sits.

Settings are a plain JSON document. Unknown keys are ignored and missing keys fall
back to the defaults, so old and hand-edited files keep working.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import sys
from typing import Any

from .layout import migrate_box

log = logging.getLogger(__name__)

APP_NAME = "DroneMapOverlay"
VERSION = 2

Settings = dict[str, Any]

DEFAULTS: Settings = {
    "version": VERSION,
    "map": {
        "enabled": True,
        "box": {"anchor": ["left", "bottom"], "dx": 0.0444, "dy": 0.1468, "w": 0.2843, "h": 0.287},
        "zoom": 17,
        "orientation": "north",  # "north" (north up) or "heading" (direction of flight up)
        "style": "standard",  # "standard", "grayscale", "dark"
        "show_path": True,
        "show_full_route": False,
        "show_cone": True,
        "show_north": True,
        "show_credit": False,
    },
    "stats": {
        "enabled": True,
        "box": {"anchor": ["left", "bottom"], "dx": 0.0444, "dy": 0.0352, "w": 0.2843, "h": 0.1},
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
        "units": "metric",  # "metric" or "imperial"
    },
    "output": {
        "codec": "auto",  # "auto" (match source, HEVC/H.264) or "h264" (most compatible)
        "folder": "",  # "" = next to the source video
        "suffix": "_map",
    },
    "app": {
        "last_video": "",  # used for the settings preview
        "theme": "dark",  # "dark", "light" or "system"
    },
    "tiles": {
        # Any XYZ tile server. "placeholder" draws an offline stand-in (tests, no network).
        "url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "credit": "© OpenStreetMap contributors",
    },
}


def _platform_dir(kind: str) -> str:
    home = os.path.expanduser("~")
    if sys.platform == "win32":
        env = "APPDATA" if kind == "config" else "LOCALAPPDATA"
        base = os.environ.get(env) or home
        return os.path.join(base, APP_NAME, "" if kind == "config" else kind)
    if sys.platform == "darwin":
        sub = {"config": "Library/Application Support", "cache": "Library/Caches", "logs": "Library/Logs"}[kind]
        return os.path.join(home, sub, APP_NAME)
    env, fallback = {
        "config": ("XDG_CONFIG_HOME", ".config"),
        "cache": ("XDG_CACHE_HOME", ".cache"),
        "logs": ("XDG_STATE_HOME", ".local/state"),
    }[kind]
    return os.path.join(os.environ.get(env) or os.path.join(home, fallback), APP_NAME)


def config_dir() -> str:
    return _platform_dir("config").rstrip("\\/")


def cache_dir() -> str:
    return _platform_dir("cache")


def log_dir() -> str:
    return _platform_dir("logs")


def _merge(base: Settings, over: Settings) -> Settings:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict) and k != "box":
            out[k] = _merge(out[k], v)
        elif k in out:
            out[k] = copy.deepcopy(v)
    return out


def normalize(raw: Settings) -> Settings:
    """Merge onto the defaults and upgrade anything stored in an older format."""
    s = _merge(DEFAULTS, raw if isinstance(raw, dict) else {})
    for name in ("map", "stats"):
        s[name]["box"] = migrate_box(s[name].get("box"), DEFAULTS[name]["box"])
    try:
        s["map"]["zoom"] = int(min(19, max(3, int(s["map"]["zoom"]))))
    except (TypeError, ValueError):
        s["map"]["zoom"] = DEFAULTS["map"]["zoom"]
    s["version"] = VERSION
    return s


def defaults() -> Settings:
    return copy.deepcopy(DEFAULTS)


def settings_path() -> str:
    return os.path.join(config_dir(), "settings.json")


def load_file(path: str) -> Settings:
    with open(path, encoding="utf-8") as f:
        return normalize(json.load(f))


def load() -> Settings:
    try:
        return load_file(settings_path())
    except FileNotFoundError:
        return defaults()
    except (OSError, ValueError) as e:
        log.warning("Settings file unreadable (%s); using defaults", e)
        return defaults()


def save(s: Settings) -> None:
    os.makedirs(config_dir(), exist_ok=True)
    tmp = settings_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(normalize(s), f, indent=2)
    os.replace(tmp, settings_path())
