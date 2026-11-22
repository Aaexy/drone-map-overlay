# PyInstaller spec for Drone Map Overlay.   Build:  pyinstaller packaging/dronemap.spec
import os
import sys

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))
from dronemap import __version__  # noqa: E402

NAME = "Drone Map Overlay"
ICON = os.path.join(ROOT, "src", "dronemap", "assets", "icon.png")

a = Analysis(
    [os.path.join(SPECPATH, "launcher.py")],
    pathex=[os.path.join(ROOT, "src")],
    datas=[(os.path.join(ROOT, "src", "dronemap", "assets"), os.path.join("dronemap", "assets"))],
    hiddenimports=["dronemap.app", "dronemap.app.main_window", "dronemap.cli", "certifi"],
    excludes=["tkinter", "numpy", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
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
