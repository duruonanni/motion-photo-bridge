"""PyInstaller and direct-script entry point for the console application."""

from __future__ import annotations

if __package__:
    from .cli import main
else:  # Support direct execution and PyInstaller's script bootstrap.
    import os
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(os.path.abspath(__file__)).parents[1]))
    from vivo_motion_photo.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
