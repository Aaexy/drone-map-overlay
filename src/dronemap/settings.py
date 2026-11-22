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
VERSION = 3

Settings = dict[str, Any]

MAP_STYLES = ("light", "dark", "color", "satellite")
_OSM = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
_OSM_CREDIT = "\u00a9 OpenStreetMap contributors"
STATS_FIELDS = ("speed", "altitude", "vspeed", "heading", "distance", "home", "time", "coords")

DEFAULTS: Settings = {
    "version": VERSION,
    "map": {
        "enabled": True,
        "box": {"anchor": ["left", "bottom"], "dx": 0.0444, "dy": 0.1468, "w": 0.2843, "h": 0.287},
        "zoom": 17,
        "orientation": "north",  # "north" (north up) or "heading" (direction of flight up)
        "style": "light",  # "light" / "dark" (grayscale), "color" (standard map), "satellite"
        "shape": "rounded",  # "rounded" or "circle"
        "opacity": 1.0,
        "trail": "full",  # "full" (whole flight so far), "recent" (last trail_seconds), "off"
        "trail_seconds": 30,
        "marker": "arrow",  # "arrow" or "dot"
        "show_full_route": False,
        "show_cone": True,
        "show_north": True,
        "show_border": True,
        "show_credit": False,
    },
    "stats": {
        "enabled": True,
        "box": {"anchor": ["left", "bottom"], "dx": 0.0444, "dy": 0.0352, "w": 0.2843, "h": 0.1},
        "layout": "row",  # "row" (side by side) or "column" (stacked)
        "theme": "dark",  # "dark" or "light" panel
        "speed": True,
        "altitude": True,
        "vspeed": False,  # vertical speed
        "heading": True,
        "distance": False,  # distance flown
        "home": False,  # straight-line distance from where the clip starts
        "time": False,  # time since the start of the clip
        "coords": True,
    },
    "style": {
        "accent": "#ff5c36",
        "panel_opacity": 0.8,
        "roundness": 0.06,
        "shadow": True,
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
        # One XYZ tile source per map style. "offline" draws a stand-in map (tests, no network).
        "offline": False,
        # light and dark are grayscale versions of the standard map, made on this computer.
        "light": {"url": _OSM, "credit": _OSM_CREDIT},
        "dark": {"url": _OSM, "credit": _OSM_CREDIT},
        "color": {"url": _OSM, "credit": _OSM_CREDIT},
        "satellite": {
            "url": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
            "credit": "Imagery \u00a9 Esri, Maxar, Earthstar Geographics",
        },
    },
}

# Map style names used by earlier versions.
_OLD_STYLES = {"standard": "color", "grayscale": "light"}


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
    raw = raw if isinstance(raw, dict) else {}
    s = _merge(DEFAULTS, raw)
    for name in ("map", "stats"):
        s[name]["box"] = migrate_box(s[name].get("box"), DEFAULTS[name]["box"])
    m = s["map"]
    m["style"] = _OLD_STYLES.get(m["style"], m["style"])
    if m["style"] not in MAP_STYLES:
        m["style"] = DEFAULTS["map"]["style"]
    old_map = raw.get("map") if isinstance(raw.get("map"), dict) else {}
    if old_map.get("show_path") is False and "trail" not in old_map:  # v2 had an on/off path switch
        m["trail"] = "off"
    old_tiles = raw.get("tiles") if isinstance(raw.get("tiles"), dict) else {}
    if isinstance(old_tiles.get("url"), str):  # v1/v2 had a single tile source
        if old_tiles["url"] == "placeholder":
            s["tiles"]["offline"] = True
        elif old_tiles["url"] != _OSM:
            for st in ("light", "dark", "color"):
                s["tiles"][st]["url"] = old_tiles["url"]
    m["zoom"] = _clamp_int(m.get("zoom"), 3, 19, DEFAULTS["map"]["zoom"])
    m["trail_seconds"] = _clamp_int(m.get("trail_seconds"), 1, 3600, DEFAULTS["map"]["trail_seconds"])
    s["version"] = VERSION
    return s


def _clamp_int(v: Any, lo: int, hi: int, default: int) -> int:
    try:
        return int(min(hi, max(lo, int(v))))
    except (TypeError, ValueError):
        return default


def first_run_defaults(system_dark: bool) -> Settings:
    """Settings for a brand-new install: the map matches the computer's light/dark mode."""
    s = defaults()
    s["map"]["style"] = "dark" if system_dark else "light"
    return s


def exists() -> bool:
    return os.path.exists(settings_path())


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
