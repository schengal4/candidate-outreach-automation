"""Per-candidate Sent List, backed by SQLite (see app/db.py).

Spec columns: candidate_id, company_domain, contact_name, contact_email,
date_sent, interview_arranged.

Two implementation columns are added on top of the spec:
  confirmed_sent — False when an entry was auto-added at draft generation and
  the candidate hasn't yet confirmed they actually sent it (drives the
  "Did you send to these?" reconciliation nudge).
  permanently_excluded — set manually when a contact asks not to be contacted
  again. Blocks outreach forever, overriding the retention window regardless
  of date_sent, and is never pruned.

Every entry has a stable integer `id` (the DB primary key) and all edits
address entries by that id, scoped to the candidate. The legacy CSV store
addressed rows by list index, which could hit the wrong row when a running
pipeline auto-added entries while the user edited the table.
"""

from datetime import date, timedelta
from typing import Dict, List, Set

from . import db

# Columns exposed on the entry dicts (plus "id").
FIELDNAMES = [
    "candidate_id",
    "company_domain",
    "contact_name",
    "contact_email",
    "date_sent",
    "interview_arranged",
    "confirmed_sent",
    "permanently_excluded",
]

_BOOL_FIELDS = {"interview_arranged", "confirmed_sent", "permanently_excluded"}


def _row_to_entry(row) -> Dict:
    e = {k: row[k] for k in ("id", *FIELDNAMES)}
    for k in _BOOL_FIELDS:
        e[k] = bool(e[k])
    return e


def load_entries(candidate_id: str) -> List[Dict]:
    rows = db.query(
        f"SELECT id, {', '.join(FIELDNAMES)} FROM sent_entries"
        " WHERE candidate_id = ? ORDER BY id",
        (candidate_id,),
    )
    return [_row_to_entry(r) for r in rows]


def add_entry(
    candidate_id: str,
    company_domain: str,
    contact_name: str,
    contact_email: str,
    date_sent: str = "",
    confirmed_sent: bool = False,
    permanently_excluded: bool = False,
) -> int:
    """Append an entry; returns its id. Defaults fit the auto-add-on-draft-
    generation path; manual adds (contact reached outside the app) pass
    confirmed_sent=True and optionally a past date_sent."""
    cur = db.execute(
        "INSERT INTO sent_entries (candidate_id, company_domain, contact_name,"
        " contact_email, date_sent, interview_arranged, confirmed_sent,"
        " permanently_excluded) VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
        (
            candidate_id,
            company_domain,
            contact_name,
            contact_email,
            date_sent or date.today().isoformat(),
            int(confirmed_sent),
            int(permanently_excluded),
        ),
    )
    return cur.lastrowid


def _is_active(entry: Dict, retention_months: int) -> bool:
    """True if the entry still blocks outreach: (today - date_sent) < retention,
    or forever if the contact asked never to be contacted again. Also keeps the
    entry out of prune_expired, so a permanent exclusion can't be aged away."""
    if entry.get("permanently_excluded"):
        return True
    try:
        sent = date.fromisoformat(str(entry.get("date_sent", "")).strip())
    except ValueError:
        return True  # unparseable date: err on the side of blocking
    return (date.today() - sent) < timedelta(days=retention_months * 30)


def active_blocked_emails(candidate_id: str, retention_months: int) -> Set[str]:
    """Emails that must not be contacted again yet (exact contact_email match)."""
    return {
        e["contact_email"].strip().lower()
        for e in load_entries(candidate_id)
        if e.get("contact_email") and _is_active(e, retention_months)
    }


def active_entries(candidate_id: str, retention_months: int) -> List[Dict]:
    return [e for e in load_entries(candidate_id) if _is_active(e, retention_months)]


def pending_confirmation(candidate_id: str) -> List[Dict]:
    return [e for e in load_entries(candidate_id) if not e["confirmed_sent"]]


def update_entry(candidate_id: str, entry_id: int, **changes) -> None:
    """Update one entry by id. The candidate_id guard means a forged or stale
    id can never edit another candidate's entry."""
    fields = [k for k in changes if k in FIELDNAMES and k != "candidate_id"]
    if not fields:
        return
    assignments = ", ".join(f"{k} = ?" for k in fields)
    values = [
        int(changes[k]) if k in _BOOL_FIELDS else changes[k] for k in fields
    ]
    db.execute(
        f"UPDATE sent_entries SET {assignments} WHERE id = ? AND candidate_id = ?",
        (*values, entry_id, candidate_id),
    )


def remove_entry(candidate_id: str, entry_id: int) -> None:
    db.execute(
        "DELETE FROM sent_entries WHERE id = ? AND candidate_id = ?",
        (entry_id, candidate_id),
    )


def remove_unconfirmed(candidate_id: str, entry_ids: List[int]) -> None:
    """Delete the given entries only if still unconfirmed — the "No, I didn't
    send those" answer to the reconciliation nudge. Scoping to the ids shown
    in the nudge (not all unconfirmed rows) means a pipeline appending new
    entries concurrently can't have them silently deleted."""
    if not entry_ids:
        return
    placeholders = ", ".join("?" for _ in entry_ids)
    db.execute(
        f"DELETE FROM sent_entries WHERE id IN ({placeholders})"
        " AND candidate_id = ? AND confirmed_sent = 0",
        (*entry_ids, candidate_id),
    )


def confirm_all(candidate_id: str) -> None:
    db.execute(
        "UPDATE sent_entries SET confirmed_sent = 1 WHERE candidate_id = ?",
        (candidate_id,),
    )


def delete_list(candidate_id: str) -> None:
    """Remove the candidate's entire sent list — only used when the profile
    itself is deleted (nothing left to run outreach for)."""
    db.execute("DELETE FROM sent_entries WHERE candidate_id = ?", (candidate_id,))


def prune_expired(candidate_id: str, retention_months: int) -> int:
    """Remove entries past the retention window. Returns count removed."""
    expired = [
        e["id"]
        for e in load_entries(candidate_id)
        if not _is_active(e, retention_months)
    ]
    for entry_id in expired:
        remove_entry(candidate_id, entry_id)
    return len(expired)
