"""Map styles, shapes, trails, markers, stats fields/layouts and settings migration."""

import pytest
from conftest import offline_cfg
from PIL import Image

from dronemap import settings as settings_mod
from dronemap import units
from dronemap.graphics import tile_style
from dronemap.overlay import Overlay
from dronemap.pipeline import demo_track
from dronemap.telemetry import Sample, Track


def map_widget(**map_opts):
    return Overlay(demo_track(), offline_cfg(map=map_opts), 1920, 1080).widgets[0].widget


def stats_widget(w_frac=0.2843, h_frac=0.1, **stats_opts):
    box = {"anchor": ["left", "bottom"], "dx": 0.04, "dy": 0.04, "w": w_frac, "h": h_frac}
    cfg = offline_cfg(map={"enabled": False}, stats={"box": box, **stats_opts})
    return Overlay(demo_track(), cfg, 1920, 1080).widgets[0].widget


# ------------------------------------------------------------------ settings
def test_first_run_follows_system_appearance():
    assert settings_mod.first_run_defaults(system_dark=True)["map"]["style"] == "dark"
    assert settings_mod.first_run_defaults(system_dark=False)["map"]["style"] == "light"


def test_default_map_is_grayscale():
    assert settings_mod.DEFAULTS["map"]["style"] in ("light", "dark")


@pytest.mark.parametrize(("old", "new"), [("standard", "color"), ("grayscale", "light"), ("dark", "dark"), ("bogus", "light")])
def test_old_map_styles_migrate(old, new):
    assert settings_mod.normalize({"map": {"style": old}})["map"]["style"] == new


def test_v2_settings_migrate():
    s = settings_mod.normalize({"map": {"show_path": False}, "tiles": {"url": "https://tiles.example/{z}/{x}/{y}.png"}})
    assert s["map"]["trail"] == "off"
    assert all(s["tiles"][st]["url"] == "https://tiles.example/{z}/{x}/{y}.png" for st in ("light", "dark", "color"))
    assert "satellite" in s["tiles"]["satellite"]["url"] or "arcgis" in s["tiles"]["satellite"]["url"]
    assert settings_mod.normalize({"tiles": {"url": "placeholder"}})["tiles"]["offline"] is True


# ------------------------------------------------------------------ map
@pytest.mark.parametrize("style", settings_mod.MAP_STYLES)
@pytest.mark.parametrize("shape", ["rounded", "circle"])
@pytest.mark.parametrize("marker", ["arrow", "dot"])
@pytest.mark.parametrize("trail", ["full", "recent", "off"])
def test_every_map_option_renders(style, shape, marker, trail):
    w = map_widget(style=style, shape=shape, marker=marker, trail=trail, show_credit=True, show_full_route=True)
    img = w.render(20.0)
    assert img.size == (w.w + 2 * w.margin, w.h + 2 * w.margin)


def test_circle_corners_are_transparent():
    w = map_widget(shape="circle")
    img = w.render(5.0)
    m = w.margin
    assert img.getpixel((m + 3, m + 3))[3] == img.getpixel((m + w.w - 4, m + w.h - 4))[3] == 0
    assert img.getpixel((m + w.w // 2, m + w.h // 2))[3] == 255


def test_opacity_makes_the_map_see_through():
    solid, faded = map_widget(opacity=1.0), map_widget(opacity=0.5)
    c = (solid.margin + solid.w // 4, solid.margin + solid.h // 4)
    assert solid.render(5.0).getpixel(c)[3] == 255
    assert 100 < faded.render(5.0).getpixel(c)[3] < 160


def accent_pixels(img: Image.Image) -> int:
    return sum(1 for r, g, b, a in img.getdata() if a > 200 and r > 220 and 70 < g < 120 and b < 90)


def test_recent_trail_is_shorter_than_full_trail():
    t = 25.0
    full = accent_pixels(map_widget(trail="full", show_cone=False).render(t))
    recent = accent_pixels(map_widget(trail="recent", trail_seconds=3, show_cone=False).render(t))
    off = accent_pixels(map_widget(trail="off", show_cone=False).render(t))
    assert full > recent > off


def test_light_and_dark_tile_styles():
    land, road = (242, 239, 233), (255, 255, 255)
    tile = Image.new("RGB", (2, 1))
    tile.putpixel((0, 0), land)
    tile.putpixel((1, 0), road)
    light, dark = tile_style("light")(tile), tile_style("dark")(tile)
    assert light.getpixel((0, 0))[0] >= 245 and light.getpixel((1, 0))[0] == 255  # near-white land
    assert dark.getpixel((0, 0))[0] < 40 and dark.getpixel((1, 0))[0] > dark.getpixel((0, 0))[0] + 30  # roads lighter
    assert tile_style("color") is None and tile_style("satellite") is None


# ------------------------------------------------------------------ stats
ALL_FIELDS = dict(speed=True, altitude=True, vspeed=True, heading=True, distance=True, home=True, time=True, coords=True)


@pytest.mark.parametrize("layout", ["row", "column"])
@pytest.mark.parametrize("theme", ["dark", "light"])
def test_stats_layouts_and_themes_render(layout, theme):
    w = stats_widget(0.4, 0.3, layout=layout, theme=theme, **ALL_FIELDS)
    img = w.render(12.0)
    assert img.size == (w.w + 2 * w.margin, w.h + 2 * w.margin)
    centre = img.getpixel((w.margin + w.w // 2, w.margin + 3))
    assert (centre[0] > 200) == (theme == "light")


def test_row_layout_wraps_into_a_grid_when_crowded():
    w = stats_widget(0.2843, 0.2, **ALL_FIELDS)
    assert 1 < w.cols < 7
    roomy = stats_widget(1.2, 0.12, speed=True, altitude=True, heading=True)
    assert roomy.cols == 3


def test_coordinates_never_outgrow_the_numbers():
    w = stats_widget(**ALL_FIELDS)
    assert w.crd_px <= w.val_px * 0.55 + 1e-6


def test_stats_values():
    w = stats_widget(**ALL_FIELDS)
    cur, _, dist = w.track.at(10.0)
    labels = [lab for lab, _, _ in w.values(cur, dist, 10.0)[0]]
    assert labels == ["SPEED", "ALTITUDE", "VERT. SPEED", "HEADING", "DISTANCE", "FROM START", "TIME"]
    assert dict((lab, v) for lab, v, _ in w.values(cur, dist, 10.0)[0])["TIME"] == "0:10"


def test_vertical_speed_from_altitude():
    s = [Sample(i * 0.5, 46.0, 7.0 + i * 1e-5, alt=100 + i * 1.0) for i in range(10)]  # climbing 2 m/s
    assert Track(s, "x").at(2.0)[0].vs == pytest.approx(2.0, rel=0.01)


def test_new_units():
    assert units.vspeed(1.23, "metric") == ("+1.2", "m/s")
    assert units.vspeed(-2.0, "imperial") == ("-4.5", "mph")
    assert units.vspeed(0.01, "metric") == ("0.0", "m/s")
    assert units.elapsed(75) == ("1:15", "")
    assert units.elapsed(3725) == ("1:02:05", "")
