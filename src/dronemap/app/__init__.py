"""The desktop app (PySide6)."""

from __future__ import annotations

import logging
import os
import sys
from typing import Optional

from PySide6.QtCore import QEvent, QTimer, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from .. import logs
from .. import settings as settings_mod

log = logging.getLogger(__name__)


class App(QApplication):
    file_opened = Signal(str)

    def event(self, e) -> bool:
        # macOS: files dropped on the Dock icon or opened with "Open With"
        if e.type() == QEvent.Type.FileOpen:
            self.file_opened.emit(e.file())
            return True
        return super().event(e)


def run(files: Optional[list[str]] = None) -> int:
    logs.setup()
    from . import theme
    from .main_window import APP_TITLE, MainWindow, videos_in
    from .platform import asset

    app = App.instance() or App(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setOrganizationName(settings_mod.APP_NAME)
    if not settings_mod.exists():
        # First launch: a white map on a light-mode computer, a black one in dark mode.
        dark = theme.resolve("system") == "dark"
        settings_mod.save(settings_mod.first_run_defaults(system_dark=dark))
        log.info("First launch: system is %s, map style set to %s", "dark" if dark else "light", "dark" if dark else "light")
    theme.apply(app, settings_mod.load()["app"].get("theme", "dark"))
    icon = asset("icon.png")
    if icon:
        app.setWindowIcon(QIcon(icon))

    def excepthook(etype, value, tb):  # last resort: log instead of silently dying
        log.critical("Unhandled error", exc_info=(etype, value, tb))

    sys.excepthook = excepthook

    w = MainWindow()
    app.file_opened.connect(lambda p: w.add_files(videos_in([p])))
    w.show()
    queued = videos_in([f for f in (files or []) if os.path.exists(f)])
    if queued:
        QTimer.singleShot(200, lambda: w.add_files(queued))
    return app.exec()


def main() -> int:
    """Entry point of the app (packaged or installed): `--cli ...` runs the command line,
    anything else opens the window and queues any video files given."""
    args = sys.argv[1:]
    if "--cli" in args:
        args.remove("--cli")
        from ..cli import main as cli

        return cli(args)
    return run(args)
