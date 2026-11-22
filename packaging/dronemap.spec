# PyInstaller spec for Drone Map Overlay.   Build:  pyinstaller packaging/dronemap.spec
import os
import sys

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))
from dronemap import __version__  # noqa: E402

NAME = "Drone Map Overlay"
ICON = os.path.join(ROOT, "src", "dronemap", "assets", "icon.png")

def license_files():
    """LICENSE, third-party notices and each bundled library's own license text."""
    import importlib.metadata as md

    out = [
        (os.path.join(ROOT, "LICENSE"), "licenses"),
        (os.path.join(ROOT, "THIRD_PARTY_LICENSES.md"), "licenses"),
        (os.path.join(SPECPATH, "licenses", "LGPL-3.0.txt"), "licenses"),
    ]
    import sysconfig

    py_license = os.path.join(sysconfig.get_paths()["stdlib"], "LICENSE.txt")
    if os.path.exists(py_license):
        out.append((py_license, os.path.join("licenses", "python")))
    for dist in ("av", "pillow", "certifi", "numpy"):
        try:
            d = md.distribution(dist)
        except md.PackageNotFoundError:
            continue
        for f in d.files or []:
            name = str(f)
            if "/licenses/" in name and not name.endswith((".py", ".pyc")):
                out.append((str(f.locate()), os.path.join("licenses", dist)))
    return out


a = Analysis(
    [os.path.join(SPECPATH, "launcher.py")],
    pathex=[os.path.join(ROOT, "src")],
    datas=[(os.path.join(ROOT, "src", "dronemap", "assets"), os.path.join("dronemap", "assets"))] + license_files(),
    hiddenimports=["dronemap.app", "dronemap.app.main_window", "dronemap.cli", "certifi"],
    excludes=["tkinter", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
              "PySide6.QtQml", "PySide6.QtQuick", "PySide6.Qt3DCore", "PySide6.QtMultimedia",
              "PySide6.QtPdf", "PySide6.QtCharts", "PySide6.QtDataVisualization"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name=NAME,
    console=False,
    icon=ICON,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name=NAME, upx=False)

# Also put the license and notices next to the executable, where people look for them.
import shutil  # noqa: E402

for fname in ("LICENSE", "THIRD_PARTY_LICENSES.md"):
    shutil.copy(os.path.join(ROOT, fname), os.path.join(DISTPATH, NAME, fname))

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name=NAME + ".app",
        icon=ICON,
        bundle_identifier="io.github.dronemapoverlay",
        version=__version__,
        info_plist={
            "CFBundleShortVersionString": __version__,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
            "CFBundleDocumentTypes": [{
                "CFBundleTypeName": "Video",
                "CFBundleTypeRole": "Viewer",
                "LSHandlerRank": "Alternate",
                "LSItemContentTypes": ["public.mpeg-4", "com.apple.quicktime-movie", "public.movie"],
            }],
        },
    )
