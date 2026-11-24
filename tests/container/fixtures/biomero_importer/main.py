# Copyright (c) 2026, NL-BioImaging. GNU GPL version 2.
"""Receive Docker's stop signal without connecting to application services."""

from pathlib import Path
import signal
from types import FrameType


def stop(signum: int, frame: FrameType | None) -> None:
    """Record the delivered signal and exit cleanly."""
    Path("/tmp/importer-stop-signal").write_text(str(signum), encoding="utf-8")
    raise SystemExit(0)


signal.signal(signal.SIGTERM, stop)
Path("/tmp/importer-signal-ready").touch()
while True:
    signal.pause()
