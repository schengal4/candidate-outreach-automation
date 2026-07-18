"""Run the whole test suite: `python tests/run_all.py` (venv active).

Each test file is a self-contained script (asserts + PASS prints) run in its
own subprocess so module-level monkeypatching in one file can't leak into
another. Exit code is nonzero if any file fails.

The suite runs against a throwaway COPY of data/ (via the DATA_DIR env var):
tests that read the owner's real profile still see it, but every write —
test candidates, sent-list entries, run-ledger rows, cleanup deletes — lands
in the copy and is discarded. The suite used to run against the live
database, and test activity during development damaged real sent-list data.
"""

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent

# Tests import the real app modules, which configure the real rotating log
# file — without this, every suite run dumps fake logins, test candidates,
# and deliberate "kaboom" tracebacks into data/logs/app.log, where they read
# as production failures. Point the whole suite at a throwaway directory.
TEST_LOG_DIR = tempfile.mkdtemp(prefix="outreach-test-logs-")


def make_test_data_dir() -> str:
    """A disposable copy of data/ for the suite to run against. The SQLite
    file is copied with the backup API, which is safe even while the real
    app is running (a plain file copy of a WAL-mode DB can tear)."""
    test_dir = Path(tempfile.mkdtemp(prefix="outreach-test-data-"))
    src_dir = TESTS_DIR.parent / "data"
    if src_dir.is_dir():
        for item in src_dir.iterdir():
            if item.name.startswith("app.db") or item.name == "logs":
                continue  # DB copied below; logs redirected separately
            if item.is_dir():
                shutil.copytree(item, test_dir / item.name)
            else:
                shutil.copy2(item, test_dir / item.name)
        db_path = src_dir / "app.db"
        if db_path.exists():
            src = sqlite3.connect(str(db_path))
            dst = sqlite3.connect(str(test_dir / "app.db"))
            with dst:
                src.backup(dst)
            dst.close()
            src.close()
    return str(test_dir)


TEST_DATA_DIR = make_test_data_dir()

# Files that exercise routes without logging in — run with the wall down.
NEEDS_OPEN_MODE = {
    "test_batch_save.py",
    "test_timeout_routes.py",
    "test_bench_backfill.py",
    "test_claim_revision.py",
    "test_draft_verification.py",
    "test_failed_retry_prompt.py",
    "test_verification_caveat.py",
}

# Excluded from the default sweep (hit real external services):
#   - Gmail draft round-trips need a live OAuth token and touch a real inbox.
#   - LLM tests against the real API cost money.
# (None are in this folder; noted so nobody adds one without a flag.)


def main() -> int:
    files = sorted(
        f for f in TESTS_DIR.glob("test_*.py")
    )
    failures = []
    total_pass = 0
    started = time.time()
    for f in files:
        env = dict(os.environ)
        env["LOG_DIR"] = TEST_LOG_DIR
        env["DATA_DIR"] = TEST_DATA_DIR
        if f.name in NEEDS_OPEN_MODE:
            env["REQUIRE_LOGIN"] = "0"
        result = subprocess.run(
            [sys.executable, str(f)],
            capture_output=True, text=True, env=env,
            cwd=str(TESTS_DIR.parent),
        )
        passes = result.stdout.count("PASS:")
        total_pass += passes
        if result.returncode != 0:
            failures.append(f.name)
            print(f"FAIL  {f.name}  ({passes} passed before failure)")
            tail = (result.stdout + result.stderr).strip().splitlines()
            for line in tail[-12:]:
                print(f"      {line}")
        else:
            print(f"ok    {f.name}  ({passes} checks)")
    elapsed = time.time() - started
    print(f"\n{len(files) - len(failures)}/{len(files)} files, "
          f"{total_pass} checks passed in {elapsed:.1f}s")
    if failures:
        print("FAILED:", ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
