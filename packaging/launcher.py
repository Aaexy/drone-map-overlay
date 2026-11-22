"""Entry point for the packaged app (PyInstaller).

Drone Map Overlay                 -> opens the app
Drone Map Overlay video.mp4 ...   -> opens the app and queues the videos
Drone Map Overlay --cli ...       -> command-line mode (same options as `dronemap`)
"""

import sys


def main():
    args = sys.argv[1:]
    if args and args[0] == "--cli":
        from dronemap.cli import main as cli

        return cli(args[1:])
    from dronemap.app import run

    return run(args)


if __name__ == "__main__":
    sys.exit(main())
