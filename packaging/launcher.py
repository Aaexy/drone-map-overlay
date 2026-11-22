"""Entry point for the packaged app (PyInstaller).

Drone Map Overlay                 -> opens the app
Drone Map Overlay video.mp4 ...   -> opens the app and queues the videos
Drone Map Overlay --cli ...       -> command-line mode (same options as `dronemap`)
"""

import sys

from dronemap.app import main

if __name__ == "__main__":
    sys.exit(main())
