#!/usr/bin/env python3
"""Apply the bridge to one vivo pair inside Immich (now `mpb apply-pair`).

Kept as a thin wrapper for existing scripts/cron entries.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from motion_photo_bridge.immich_apply import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
