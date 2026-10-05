"""Consistent SQLite backup using the online backup API.

    docker compose exec app python -m cyfun.backup

Writes DATA_DIR/backups/cyfun-YYYYmmdd-HHMMSS.sqlite3 and keeps the last 14 files.
Evidence files and connector snapshots live next to the database in DATA_DIR; back up
the whole volume (for example with a nightly tar of the volume or a Proxmox backup job).
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime

from .config import get_settings


def main() -> int:
    s = get_settings()
    out_dir = s.data_dir / "backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    target = out_dir / f"cyfun-{stamp}.sqlite3"
    src = sqlite3.connect(f"file:{s.db_path.as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(target.as_posix())
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    backups = sorted(out_dir.glob("cyfun-*.sqlite3"))
    for old in backups[:-14]:
        old.unlink()
    print(f"backup written: {target} ({target.stat().st_size} bytes); {min(len(backups), 14)} kept")
    return 0


if __name__ == "__main__":
    sys.exit(main())
