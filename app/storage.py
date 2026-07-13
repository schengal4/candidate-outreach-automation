"""Candidate persistence, backed by SQLite (see app/db.py).

Candidates are stored as one JSON document per row (the model's own
to_dict/from_dict tolerance handles schema evolution, same as the legacy
candidates.json did) plus an indexed, normalized owner_email column for the
per-account listing query. The old mtime-keyed parse cache and RLock are
gone — SQLite serves the per-request reads and serializes writers itself.
"""

import json
from typing import List, Optional

from . import db
from .models import Candidate


def list_candidates(owner_email: Optional[str] = None) -> List[Candidate]:
    """All candidates, or only those owned by `owner_email` when given."""
    if owner_email is None:
        rows = db.query("SELECT data FROM candidates")
    else:
        rows = db.query(
            "SELECT data FROM candidates WHERE owner_email = ?",
            (owner_email.strip().lower(),),
        )
    return [Candidate.from_dict(json.loads(r["data"])) for r in rows]


def get_candidate(candidate_id: str) -> Optional[Candidate]:
    rows = db.query("SELECT data FROM candidates WHERE id = ?", (candidate_id,))
    return Candidate.from_dict(json.loads(rows[0]["data"])) if rows else None


def save_candidate(candidate: Candidate) -> None:
    db.execute(
        "INSERT OR REPLACE INTO candidates (id, owner_email, data) VALUES (?, ?, ?)",
        (
            candidate.id,
            candidate.owner_email.strip().lower(),
            json.dumps(candidate.to_dict(), ensure_ascii=False),
        ),
    )


def delete_candidate(candidate_id: str) -> None:
    db.execute("DELETE FROM candidates WHERE id = ?", (candidate_id,))
