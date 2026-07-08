"""Per-candidate Sent List stored as CSV.

Spec columns: candidate_id, company_domain, contact_name, contact_email,
date_sent, interview_arranged.

Two implementation columns are added on top of the spec:
  confirmed_sent — False when an entry was auto-added at draft generation and
  the candidate hasn't yet confirmed they actually sent it (drives the
  "Did you send to these?" reconciliation nudge).
  permanently_excluded — set manually when a contact asks not to be contacted
  again. Blocks outreach forever, overriding the retention window regardless
  of date_sent, and is never pruned.
"""

import csv
import io
from datetime import date, timedelta
from typing import Dict, List, Set

from .config import DATA_DIR
from .fsutil import atomic_write_text

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

_TRUE = {"true", "1", "yes", "on"}


def _path(candidate_id: str):
    return DATA_DIR / f"sent_list_{candidate_id}.csv"


def _to_bool(v) -> bool:
    return str(v).strip().lower() in _TRUE


def load_entries(candidate_id: str) -> List[Dict]:
    p = _path(candidate_id)
    if not p.exists():
        return []
    with open(p, "r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["interview_arranged"] = _to_bool(r.get("interview_arranged"))
        r["confirmed_sent"] = _to_bool(r.get("confirmed_sent"))
        # missing in CSVs written before the column existed -> False
        r["permanently_excluded"] = _to_bool(r.get("permanently_excluded"))
    return rows


def save_entries(candidate_id: str, entries: List[Dict]) -> None:
    # Render to memory, then swap into place atomically — the sent list is
    # the do-not-recontact record, the last file we can afford to corrupt.
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=FIELDNAMES, lineterminator="\r\n")
    writer.writeheader()
    for e in entries:
        writer.writerow({k: e.get(k, "") for k in FIELDNAMES})
    atomic_write_text(_path(candidate_id), buf.getvalue())


def add_entry(
    candidate_id: str,
    company_domain: str,
    contact_name: str,
    contact_email: str,
    date_sent: str = "",
    confirmed_sent: bool = False,
    permanently_excluded: bool = False,
) -> None:
    """Append an entry. Defaults fit the auto-add-on-draft-generation path;
    manual adds (contact reached outside the app) pass confirmed_sent=True and
    optionally a past date_sent."""
    entries = load_entries(candidate_id)
    entries.append(
        {
            "candidate_id": candidate_id,
            "company_domain": company_domain,
            "contact_name": contact_name,
            "contact_email": contact_email,
            "date_sent": date_sent or date.today().isoformat(),
            "interview_arranged": False,
            "confirmed_sent": confirmed_sent,
            "permanently_excluded": permanently_excluded,
        }
    )
    save_entries(candidate_id, entries)


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


def update_entry(candidate_id: str, index: int, **changes) -> None:
    entries = load_entries(candidate_id)
    if 0 <= index < len(entries):
        entries[index].update(changes)
        save_entries(candidate_id, entries)


def remove_entry(candidate_id: str, index: int) -> None:
    entries = load_entries(candidate_id)
    if 0 <= index < len(entries):
        entries.pop(index)
        save_entries(candidate_id, entries)


def delete_list(candidate_id: str) -> None:
    """Remove the candidate's entire sent list — only used when the profile
    itself is deleted (nothing left to run outreach for)."""
    p = _path(candidate_id)
    if p.exists():
        p.unlink()


def prune_expired(candidate_id: str, retention_months: int) -> int:
    """Remove entries past the retention window. Returns count removed."""
    entries = load_entries(candidate_id)
    kept = [e for e in entries if _is_active(e, retention_months)]
    removed = len(entries) - len(kept)
    if removed:
        save_entries(candidate_id, kept)
    return removed
