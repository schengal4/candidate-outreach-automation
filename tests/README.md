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

- `run_all.py` sets `REQUIRE_LOGIN=0` for the two files that exercise routes
  without logging in (`test_batch_save.py`, `test_timeout_routes.py`); every
  other file manages login state itself.
- A few files assume the **local data set** — they read (and carefully
  restore) the real profile `516e7c4751` in `data/candidates.json`:
  `test_isolation.py`, `test_single_profile.py`, `test_draft_instructions.py`.
  On a fresh clone with an empty `data/`, expect those to fail until a profile
  exists; everything else creates and removes its own throwaway data.
- Tests write throwaway files under `data/` (temp candidates, sent lists,
  run states) and clean them up in `finally` blocks even on failure.

## What's covered

Login wall + Google Sign-In internals, per-account isolation, one-profile
flow + profile deletion, draft instructions, run timeouts + stop button,
batch/individual save-to-Gmail, rate-limit retry/backoff, prompt caching
request shapes, effort parameter, paragraph unwrapping, storage cache +
write-lock, discovery sent-list exclusion, cumulative sent list, permanent
exclusion, run persistence/restart settling, atomic writes, data backups,
the daily run cap, and background-task error logging.
