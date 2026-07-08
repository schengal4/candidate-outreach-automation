"""Candidate persistence (JSON file).

The file is small but read on every request (page loads, ownership checks,
run-panel polls every 3s), so reads are served from an in-memory cache keyed
by the file's (mtime_ns, size) — the JSON (which embeds every candidate's
full resume text) is only re-parsed when the file actually changed, including
edits made outside this process. A lock makes read-modify-write updates
(save/delete) atomic against each other; without it two concurrent saves
could each load, mutate, and write, silently dropping one of the changes.
"""

import json
import threading
from typing import Dict, List, Optional, Tuple

from .config import DATA_DIR
from .fsutil import atomic_write_text
from .models import Candidate

CANDIDATES_FILE = DATA_DIR / "candidates.json"

# RLock: save/delete hold it across their whole read-modify-write while
# _load_all/_save_all re-acquire it internally.
_lock = threading.RLock()
_cache: Optional[Dict[str, dict]] = None
_cache_key: Optional[Tuple[int, int]] = None


def _file_key() -> Optional[Tuple[int, int]]:
    stat = CANDIDATES_FILE.stat()
    return (stat.st_mtime_ns, stat.st_size)


def _load_all() -> Dict[str, dict]:
    global _cache, _cache_key
    with _lock:
        if not CANDIDATES_FILE.exists():
            _cache, _cache_key = None, None
            return {}
        key = _file_key()
        if _cache is None or key != _cache_key:
            with open(CANDIDATES_FILE, "r", encoding="utf-8") as f:
                _cache = json.load(f)
            _cache_key = key
        # Shallow copy so callers can add/remove entries without mutating the
        # cache behind its key check (values are only read or replaced whole).
        return dict(_cache)


def _save_all(data: Dict[str, dict]) -> None:
    global _cache, _cache_key
    with _lock:
        atomic_write_text(CANDIDATES_FILE, json.dumps(data, indent=2, ensure_ascii=False))
        _cache = dict(data)
        _cache_key = _file_key()


def list_candidates(owner_email: Optional[str] = None) -> List[Candidate]:
    """All candidates, or only those owned by `owner_email` when given."""
    candidates = [Candidate.from_dict(d) for d in _load_all().values()]
    if owner_email is not None:
        owner = owner_email.strip().lower()
        candidates = [c for c in candidates if c.owner_email.strip().lower() == owner]
    return candidates


def get_candidate(candidate_id: str) -> Optional[Candidate]:
    d = _load_all().get(candidate_id)
    return Candidate.from_dict(d) if d else None


def save_candidate(candidate: Candidate) -> None:
    with _lock:
        data = _load_all()
        data[candidate.id] = candidate.to_dict()
        _save_all(data)


def delete_candidate(candidate_id: str) -> None:
    with _lock:
        data = _load_all()
        if candidate_id in data:
            del data[candidate_id]
            _save_all(data)
