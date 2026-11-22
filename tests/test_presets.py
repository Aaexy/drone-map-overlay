import json

import pytest

from dronemap import cli
from dronemap import settings as S


def test_builtin_presets_are_valid():
    presets = S.list_presets()
    names = [p["name"] for p in presets]
    assert "Default" in names and len(presets) >= 3
    for p in presets:
        assert p["builtin"]
        cfg = S.apply_preset(S.defaults(), S.load_preset(p["name"]))
        assert cfg == S.normalize(cfg)  # already in the current format


def test_default_preset_matches_defaults():
    d = S.defaults()
    assert S.apply_preset(d, S.load_preset("default")) == S.normalize(d)


def test_save_load_round_trip_keeps_only_overlay_sections():
    cfg = S.defaults()
    cfg["map"]["style"] = "satellite"
    cfg["stats"]["coords"] = False
    cfg["output"]["folder"] = "/somewhere"
    path = S.save_preset("  My look ", cfg)
    with open(path) as f:
        data = json.load(f)
    assert data["name"] == "My look"
    assert "output" not in data and "app" not in data and "tiles" not in data

    other = S.defaults()
    other["output"]["folder"] = "/elsewhere"
    applied = S.apply_preset(other, S.load_preset("my look"))  # names are case-insensitive
    assert applied["map"]["style"] == "satellite" and applied["stats"]["coords"] is False
    assert applied["output"]["folder"] == "/elsewhere"  # per-computer settings are kept


def test_user_preset_overrides_builtin_and_can_be_deleted():
    cfg = S.defaults()
    cfg["map"]["zoom"] = 14
    S.save_preset("Default", cfg)
    p = S.find_preset("Default")
    assert not p["builtin"]
    assert S.load_preset("Default")["map"]["zoom"] == 14
    assert [x["name"] for x in S.list_presets()].count("Default") == 1
    S.delete_preset("Default")
    assert S.find_preset("Default")["builtin"]
    with pytest.raises(S.PresetError):
        S.delete_preset("Default")
    with pytest.raises(S.PresetError):
        S.load_preset("nope")


def test_save_replaces_same_name_and_rename():
    cfg = S.defaults()
    a = S.save_preset("Trip", cfg)
    cfg["map"]["zoom"] = 15
    assert S.save_preset("trip", cfg) == a  # same preset, not a second file
    S.rename_preset("Trip", "Holiday")
    assert S.find_preset("Trip") is None
    assert S.load_preset("Holiday")["map"]["zoom"] == 15


def test_import_export(tmp_path):
    cfg = S.defaults()
    cfg["style"]["accent"] = "#123456"
    out = tmp_path / "shared.json"
    S.export_preset(cfg, "Shared", str(out))
    assert S.import_preset(str(out)) == "Shared"
    assert S.load_preset("Shared")["style"]["accent"] == "#123456"
    bad = tmp_path / "bad.json"
    bad.write_text('{"hello": 1}')
    with pytest.raises(S.PresetError):
        S.import_preset(str(bad))
    bad.write_text("not json")
    with pytest.raises(S.PresetError):
        S.import_preset(str(bad))


def test_old_format_preset_is_upgraded(tmp_path):
    f = tmp_path / "old.json"
    f.write_text(json.dumps({"map": {"box": [0.02, 0.6, 0.2, 0.35], "style": "grayscale", "show_path": False}}))
    cfg = S.apply_preset(S.defaults(), S.load_preset(str(f)))
    assert cfg["map"]["style"] == "light" and "anchor" in cfg["map"]["box"]


def test_cli_list_and_unknown_preset(capsys):
    assert cli.main(["--list-presets"]) == 0
    assert "Default" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["x.mp4", "--preset", "does not exist"])
