"""SQLite persistence layer — the single store behind candidates, sent lists,
and runs (see app/storage.py, app/sent_list.py, app/run_store.py).

This replaced four parallel hand-rolled stores (candidates.json + an
mtime cache, one CSV per candidate's sent list, one JSON file per run, and
the atomic-write/lock machinery each needed). SQLite gives the same
crash-safety transactionally, plus what the files couldn't: stable primary
keys for sent-list entries (editing by list index raced with concurrent
auto-adds) and a run ledger that is never pruned, so the daily cost cap no
longer depends on run-report retention keeping enough files around.

Gmail OAuth tokens deliberately stay as one file per candidate under
data/gmail_tokens/ — credentials separate from application data is a
documented compliance choice (see app/gmail_client.py).

Connection notes: one process-wide connection, guarded by an RLock (the app
is a single asyncio process; the lock covers the few worker-thread touches).
connect() re-opens automatically when config.settings.DB_PATH changes, so
tests can point at a temp directory in-process. On first connect against a
data dir that still has legacy files, their contents are imported once
(tables must be empty); the legacy files are left in place untouched.
"""

import csv
import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, List, Optional

from . import config

logger = logging.getLogger("app.db")

_lock = threading.RLock()
_conn: Optional[sqlite3.Connection] = None
_conn_path: Optional[Path] = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates (
    id          TEXT PRIMARY KEY,
    owner_email TEXT NOT NULL DEFAULT '',
    data        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_candidates_owner ON candidates(owner_email);

CREATE TABLE IF NOT EXISTS runs (
    id           TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    created_at   REAL NOT NULL,
    data         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_candidate ON runs(candidate_id);

-- One row per run ever started, never pruned: the MAX_RUNS_PER_DAY cost
-- guardrail counts this table, so deleting old run reports can't quietly
-- raise the cap.
CREATE TABLE IF NOT EXISTS run_ledger (
    candidate_id TEXT NOT NULL,
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_candidate ON run_ledger(candidate_id);

-- Durable per-company failure memory (one row per candidate+domain): written
-- when a company drops for a company-specific reason (contacts can't be
-- found/verified), cleared when a later run drafts it successfully. Feeds the
-- review gate's "failed last time" flag independently of run-report retention
-- (KEEP_RUNS_PER_CANDIDATE) — real data showed failures from 6+ runs back
-- resurfacing unflagged because the runs that recorded them had been pruned.
CREATE TABLE IF NOT EXISTS company_failures (
    candidate_id   TEXT NOT NULL,
    domain         TEXT NOT NULL,
    reason         TEXT NOT NULL,
    last_failed_at REAL NOT NULL,
    PRIMARY KEY (candidate_id, domain)
);

CREATE TABLE IF NOT EXISTS sent_entries (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id         TEXT NOT NULL,
    company_domain       TEXT NOT NULL DEFAULT '',
    contact_name         TEXT NOT NULL DEFAULT '',
    contact_email        TEXT NOT NULL DEFAULT '',
    date_sent            TEXT NOT NULL DEFAULT '',
    interview_arranged   INTEGER NOT NULL DEFAULT 0,
    confirmed_sent       INTEGER NOT NULL DEFAULT 0,
    permanently_excluded INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sent_candidate ON sent_entries(candidate_id);
"""


def connect() -> sqlite3.Connection:
    """The process-wide connection, (re)opened lazily against the current
    settings.DB_PATH. Safe to call from any thread; use execute()/query()
    below rather than touching the connection directly."""
    global _conn, _conn_path
    with _lock:
        path = Path(config.settings.DB_PATH)
        if _conn is None or _conn_path != path:
            if _conn is not None:
                _conn.close()
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            conn.commit()
            _conn, _conn_path = conn, path
            _migrate_legacy_files(conn)
        return _conn


def execute(sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
    """One write statement in its own transaction."""
    with _lock:
        conn = connect()
        with conn:
            return conn.execute(sql, params)


def query(sql: str, params: Iterable[Any] = ()) -> List[sqlite3.Row]:
    with _lock:
        return connect().execute(sql, params).fetchall()


def close() -> None:
    """Close the cached connection (tests use this between temp dirs)."""
    global _conn, _conn_path
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn, _conn_path = None, None


# ------------------------------------------------------------------ #
# One-time import of the legacy file formats
# ------------------------------------------------------------------ #
_TRUE = {"true", "1", "yes", "on"}


def _to_bool(v: Any) -> bool:
    return str(v).strip().lower() in _TRUE


def _migrate_legacy_files(conn: sqlite3.Connection) -> None:
    """Import candidates.json / sent_list_*.csv / runs/*.json into empty
    tables. Legacy files are read-only here and left in place — the startup
    backup (see app/backup.py) runs before the first connect, so the
    pre-migration state is also zipped away. Never raises: a failed import
    of one legacy file must not take down startup, and an empty table with
    the file still on disk means the import re-runs next start."""
    data_dir = Path(config.settings.DATA_DIR)
    try:
        _migrate_candidates(conn, data_dir / "candidates.json")
        _migrate_sent_lists(conn, data_dir)
        _migrate_runs(conn, data_dir / "runs")
        conn.commit()
    except Exception:
        conn.rollback()
        logger.exception("Legacy data import failed (continuing with what's in the DB)")


def _migrate_candidates(conn: sqlite3.Connection, f: Path) -> None:
    if not f.exists() or conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]:
        return
    data = json.loads(f.read_text(encoding="utf-8"))
    for cid, d in data.items():
        conn.execute(
            "INSERT OR IGNORE INTO candidates (id, owner_email, data) VALUES (?, ?, ?)",
            (cid, str(d.get("owner_email", "")).strip().lower(), json.dumps(d, ensure_ascii=False)),
        )
    logger.info("Imported %d candidate(s) from legacy candidates.json", len(data))


def _migrate_sent_lists(conn: sqlite3.Connection, data_dir: Path) -> None:
    if conn.execute("SELECT COUNT(*) FROM sent_entries").fetchone()[0]:
        return
    total = 0
    for csv_file in sorted(data_dir.glob("sent_list_*.csv")):
        candidate_id = csv_file.stem.removeprefix("sent_list_")
        with open(csv_file, "r", encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        for r in rows:
            conn.execute(
                "INSERT INTO sent_entries (candidate_id, company_domain, contact_name,"
                " contact_email, date_sent, interview_arranged, confirmed_sent,"
                " permanently_excluded) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    candidate_id,
                    r.get("company_domain", "") or "",
                    r.get("contact_name", "") or "",
                    r.get("contact_email", "") or "",
                    r.get("date_sent", "") or "",
                    int(_to_bool(r.get("interview_arranged"))),
                    int(_to_bool(r.get("confirmed_sent"))),
                    int(_to_bool(r.get("permanently_excluded"))),
                ),
            )
        total += len(rows)
    if total:
        logger.info("Imported %d sent-list entrie(s) from legacy CSVs", total)


def _migrate_runs(conn: sqlite3.Connection, runs_dir: Path) -> None:
    if not runs_dir.is_dir() or conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]:
        return
    count = 0
    for f in sorted(runs_dir.glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            run_id = str(d["id"])
            candidate_id = str(d.get("candidate_id", ""))
            created_at = float(d.get("created_at", 0.0))
        except Exception:
            logger.warning("Skipping unreadable legacy run file %s", f.name, exc_info=True)
            continue
        conn.execute(
            "INSERT OR IGNORE INTO runs (id, candidate_id, created_at, data) VALUES (?, ?, ?, ?)",
            (run_id, candidate_id, created_at, json.dumps(d, ensure_ascii=False)),
        )
        # Seed the cost-cap ledger so migrated runs still count toward today.
        conn.execute(
            "INSERT INTO run_ledger (candidate_id, created_at) VALUES (?, ?)",
            (candidate_id, created_at),
        )
        count += 1
    if count:
        logger.info("Imported %d run(s) from legacy data/runs/", count)
