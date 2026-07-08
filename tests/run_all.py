"""Run the whole test suite: `python tests/run_all.py` (venv active).

Each test file is a self-contained script (asserts + PASS prints) run in its
own subprocess so module-level monkeypatching in one file can't leak into
another. Exit code is nonzero if any file fails.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent

# Files that exercise routes without logging in — run with the wall down.
NEEDS_OPEN_MODE = {"test_batch_save.py", "test_timeout_routes.py"}

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
