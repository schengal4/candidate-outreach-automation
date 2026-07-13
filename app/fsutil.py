"""Crash-safe file writes.

Every data file in this app (candidates.json, sent-list CSVs, run states,
Gmail tokens) is small and rewritten whole. A direct open-and-write leaves a
window where a crash or power loss mid-write corrupts the only copy. These
helpers write to a temp file in the same directory, fsync it, then swap it
into place with os.replace() — which is atomic on both Windows (NTFS) and
POSIX — so readers only ever see the old complete file or the new one.
"""

import os
import tempfile
from pathlib import Path


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path = Path(path)
    # Lazy dir creation: config no longer mkdirs at import, so writers into
    # data/ subdirectories create their parent on first use.
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    atomic_write_bytes(path, text.encode(encoding))
