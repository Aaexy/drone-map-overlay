"""Number formatting for the stats panel. Each function returns (value, unit)."""

from __future__ import annotations

from typing import Optional

Formatted = tuple[str, str]


def speed(mps: Optional[float], units: str) -> Formatted:
    if mps is None:
        return "--", ""
    if units == "imperial":
        return "%.0f" % (mps * 2.236936), "mph"
    return "%.0f" % (mps * 3.6), "km/h"


def altitude(m: Optional[float], units: str) -> Formatted:
    if m is None:
        return "--", ""
    if units == "imperial":
        return "%.0f" % (m * 3.28084), "ft"
    return "%.0f" % m, "m"


def heading(deg: Optional[float]) -> Formatted:
    if deg is None:
        return "--", "°"
    return "%03.0f" % (round(deg) % 360), "°"


def distance(m: float, units: str) -> Formatted:
    if units == "imperial":
        ft = m * 3.28084
        return ("%.0f" % ft, "ft") if ft < 1000 else ("%.2f" % (m / 1609.344), "mi")
    return ("%.0f" % m, "m") if m < 1000 else ("%.2f" % (m / 1000), "km")


def coords(lat: float, lon: float) -> str:
    return "%.5f° %s   %.5f° %s" % (abs(lat), "N" if lat >= 0 else "S", abs(lon), "E" if lon >= 0 else "W")
