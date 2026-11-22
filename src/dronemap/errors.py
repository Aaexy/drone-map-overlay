"""Errors people can act on, instead of raw library messages."""

from __future__ import annotations

import errno
from typing import Optional


class UserFacingError(Exception):
    """An error whose message is written for the person using the app."""

    def __init__(self, message: str, hint: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.hint = hint


class NoTelemetryError(UserFacingError):
    def __init__(self):
        super().__init__(
            "This video has no GPS data.",
            "Recent DJI drones store GPS inside the video. For other drones, record with "
            "Video Subtitles / Captions turned on and keep the .SRT file next to the video.",
        )


class Cancelled(Exception):
    """The user cancelled the render."""


def describe(exc: BaseException) -> tuple[str, Optional[str]]:
    """(message, hint) for showing `exc` to a person."""
    if isinstance(exc, UserFacingError):
        return exc.message, exc.hint
    if isinstance(exc, FileNotFoundError):
        return (
            "The video file couldn't be found.",
            "If it's on a memory card or external drive, check that it's still connected.",
        )
    if isinstance(exc, PermissionError) or getattr(exc, "errno", None) in (errno.EACCES, errno.EROFS, errno.EPERM):
        return ("The app isn't allowed to write the new video there.", "Choose another output folder in Settings → Output.")
    if getattr(exc, "errno", None) == errno.ENOSPC:
        return "The disk is full.", "Free up space or choose another output folder in Settings → Output."
    try:
        import av

        if isinstance(exc, av.error.InvalidDataError):
            return (
                "This file couldn't be read as a video.",
                "It may be damaged or still being copied. Try copying it from the drone again.",
            )
    except Exception:  # pragma: no cover - av always importable in practice
        pass
    return "Something went wrong while making the video.", "Details were saved to the log file."


def log_failure(logger, what: str, exc: BaseException) -> None:
    """Expected problems (no GPS, disk full...) get one line; anything else gets the traceback."""
    if isinstance(exc, UserFacingError):
        logger.warning("%s: %s", what, exc.message)
    else:
        logger.error("%s", what, exc_info=exc)
