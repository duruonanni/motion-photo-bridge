"""Console-free PyInstaller entry point for the Tkinter application."""

from __future__ import annotations

if __package__:
    from .gui import main
else:  # Support ``python src/vivo_motion_photo/gui_entry.py`` and PyInstaller.
    import os
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(os.path.abspath(__file__)).parents[1]))
    from vivo_motion_photo.gui import main


if __name__ == "__main__":
    raise SystemExit(main())
