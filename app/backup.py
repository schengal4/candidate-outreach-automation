"""Startup backup of data/ — the only copy of candidates, sent lists (the
do-not-recontact record), runs, and Gmail tokens.

On every server start, if the newest backup is older than a day, zip data/
into data_backups/ (outside data/, gitignored) and keep the most recent
KEEP_BACKUPS. Small files, so this is milliseconds — cheap insurance against
a bad write, an accidental delete, or a corrupted disk sector.
"""

import logging
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import BASE_DIR, DATA_DIR

logger = logging.getLogger("app.backup")

BACKUP_DIR = BASE_DIR / "data_backups"
KEEP_BACKUPS = 14
MIN_INTERVAL_SECONDS = 24 * 3600  # at most one backup per day


def backup_data_dir(force: bool = False) -> Optional[Path]:
    """Zip data/ into data_backups/ unless a recent backup already exists.
    Returns the new backup's path, or None if skipped. Never raises — a
    failed backup must not stop the app from starting."""
    try:
        BACKUP_DIR.mkdir(exist_ok=True)
        existing = sorted(BACKUP_DIR.glob("data-*.zip"), key=lambda p: p.stat().st_mtime)
        if not force and existing:
            age = time.time() - existing[-1].stat().st_mtime
            if age < MIN_INTERVAL_SECONDS:
                return None

        target = BACKUP_DIR / f"data-{datetime.now():%Y%m%d-%H%M%S}.zip"
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(DATA_DIR.rglob("*")):
                if f.is_file():
                    zf.write(f, arcname=str(f.relative_to(DATA_DIR)))

        existing.append(target)
        for old in existing[:-KEEP_BACKUPS]:
            old.unlink()
        logger.info("Backed up data/ to %s", target.name)
        return target
    except Exception:
        logger.exception("data/ backup failed (continuing startup)")
        return None
