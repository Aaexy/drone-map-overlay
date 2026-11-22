import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "packaging"))

from dronemap import settings as settings_mod  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    """Never touch the real settings, cache or logs while testing."""
    monkeypatch.setattr(settings_mod, "_platform_dir", lambda kind: str(tmp_path / ("_" + kind)))


def offline_cfg(**over):
    cfg = settings_mod.defaults()
    cfg["tiles"]["offline"] = True
    for section, values in over.items():
        cfg[section].update(values)
    return cfg


@pytest.fixture(scope="session")
def sample_video(tmp_path_factory):
    import make_sample

    return make_sample.make(str(tmp_path_factory.mktemp("sample")))
