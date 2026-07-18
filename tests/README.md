# Tests

Run everything (with the `.env` virtualenv active, from the project root):

```powershell
python tests\run_all.py
```

Each file is a self-contained script — assertions plus `PASS:` lines — run in
its own subprocess by `run_all.py` so monkeypatching can't leak between files.
No file makes real network calls: Google OAuth, the Anthropic API, Hunter, and
Gmail are all mocked at the module boundary. Word COM does run for real in a
couple of resume tests (Windows + Word required, like the app itself).

## Environment notes

- **`run_all.py` runs the suite against a throwaway copy of `data/`** (via
  the `DATA_DIR` env var): tests that read the owner's real profile still see
  it, but every write — test candidates, sent-list entries, run-ledger rows,
  cleanup deletes — lands in the copy and is discarded. The suite used to run
  against the live database, and test activity during development damaged
  real sent-list data. Running a single test file directly
  (`python tests\test_foo.py`) does NOT get this protection — prefer
  `run_all.py`, or set `DATA_DIR` yourself.
- `run_all.py` sets `REQUIRE_LOGIN=0` for the files that exercise routes
  without logging in (see `NEEDS_OPEN_MODE` in `run_all.py`); every other
  file manages login state itself.
- A few files assume the **local data set** — they read the real profile
  `516e7c4751`: `test_isolation.py`, `test_single_profile.py`,
  `test_draft_instructions.py`. On a fresh clone with an empty `data/`,
  expect those to fail until a profile exists; everything else creates and
  removes its own throwaway data.

## What's covered

Login wall + Google Sign-In internals, per-account isolation, one-profile
flow + profile deletion, draft instructions, run timeouts + stop button,
batch/individual save-to-Gmail, rate-limit retry/backoff, connection-outage
retry (wait-for-network salvage in `ask_json`), research-failure flags (failed
passes never drop a company; failure prose never ships as research notes),
prompt caching request shapes, effort parameter, paragraph unwrapping, storage
cache + write-lock, discovery sent-list exclusion, cumulative sent list,
permanent exclusion, run persistence/restart settling, atomic writes, data
backups, the daily run cap, and background-task error logging.
