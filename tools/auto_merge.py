#!/usr/bin/env python3
"""Link + merge on a timer (now `mpb auto-merge`).

Kept as a thin wrapper for existing scripts/cron entries.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from motion_photo_bridge.auto_merge import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
