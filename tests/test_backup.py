"""Startup backup of data/: creates a zip containing the data files, skips
when a recent backup exists, and prunes old backups. Redirects BACKUP_DIR to
a temp dir so the real data_backups/ is untouched."""
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

import tempfile
import time
import zipfile
from pathlib import Path

import app.backup as backup

with tempfile.TemporaryDirectory() as td:
    backup.BACKUP_DIR = Path(td)

    # 1. First call creates a zip containing the real data files
    first = backup.backup_data_dir()
    assert first is not None and first.exists()
    names = zipfile.ZipFile(first).namelist()
    assert "candidates.json" in names, names
    print("PASS: backup zip created and contains candidates.json")

    # 2. Immediate second call is skipped (newest backup is fresh)
    assert backup.backup_data_dir() is None
    print("PASS: a fresh backup suppresses a second one (daily cadence)")

    # 3. force=True overrides the cadence
    time.sleep(1.1)  # distinct timestamped filename
    second = backup.backup_data_dir(force=True)
    assert second is not None and second != first
    print("PASS: force=True creates another backup")

    # 4. Retention prunes the oldest beyond KEEP_BACKUPS
    orig_keep = backup.KEEP_BACKUPS
    backup.KEEP_BACKUPS = 2
    try:
        time.sleep(1.1)
        third = backup.backup_data_dir(force=True)
        remaining = sorted(p.name for p in Path(td).glob("data-*.zip"))
        assert len(remaining) == 2, remaining
        assert first.name not in remaining, "oldest backup should be pruned"
        assert third.name in remaining
        print("PASS: retention keeps only the newest KEEP_BACKUPS zips")
    finally:
        backup.KEEP_BACKUPS = orig_keep

    # 5. Failures never raise (must not block startup)
    backup.BACKUP_DIR = Path(td) / "nonexistent" / "deeply" / "nested"
    orig_mkdir = Path.mkdir
    def failing_mkdir(self, *a, **kw):
        raise OSError("disk full")
    Path.mkdir = failing_mkdir
    try:
        assert backup.backup_data_dir() is None
    finally:
        Path.mkdir = orig_mkdir
    print("PASS: backup failure is swallowed (startup never blocked)")
