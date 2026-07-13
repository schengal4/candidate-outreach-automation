"""Verify permanently_excluded overrides the retention window and pruning,
using a throwaway candidate id so real data is untouched. Also checks the
legacy-CSV import path (pre-SQLite sent lists keep their flags)."""
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from app import sent_list

CID = "testpermflag"
try:
    # Two entries sent 3 years ago — far past any retention window (max 18mo).
    sent_list.add_entry(CID, "old.com", "Old Contact", "old@old.com",
                        date_sent="2023-07-06", confirmed_sent=True)
    sent_list.add_entry(CID, "never.com", "Never Contact", "never@never.com",
                        date_sent="2023-07-06", confirmed_sent=True,
                        permanently_excluded=True)

    blocked = sent_list.active_blocked_emails(CID, 12)
    assert "old@old.com" not in blocked, "expired entry should not block"
    assert "never@never.com" in blocked, "permanent exclusion must block past retention"
    print("PASS: permanent exclusion blocks past the retention window; normal entry expires")

    removed = sent_list.prune_expired(CID, 12)
    entries = sent_list.load_entries(CID)
    assert removed == 1 and len(entries) == 1
    assert entries[0]["contact_email"] == "never@never.com"
    print("PASS: prune removed the expired entry, kept the permanently excluded one")

    # Toggle off (by stable entry id) -> the same old entry now expires
    sent_list.update_entry(CID, entries[0]["id"], permanently_excluded=False)
    assert "never@never.com" not in sent_list.active_blocked_emails(CID, 12)
    print("PASS: toggling the flag off restores normal retention behaviour")

    # An edit addressed to the wrong candidate must not touch the entry
    sent_list.update_entry("someoneelse", entries[0]["id"], permanently_excluded=True)
    assert "never@never.com" not in sent_list.active_blocked_emails(CID, 12)
    print("PASS: entry ids are scoped to their candidate")
finally:
    sent_list.delete_list(CID)
    print("cleanup: removed throwaway sent list")

# Legacy CSVs (pre-SQLite) import on first connect; files without the
# permanently_excluded column load with the flag False.
import pathlib
import tempfile

from app import config, db

_original_dir = config.settings.DATA_DIR
db.close()
tmp = pathlib.Path(tempfile.mkdtemp())
(tmp / "sent_list_legacycid.csv").write_text(
    "candidate_id,company_domain,contact_name,contact_email,date_sent,interview_arranged,confirmed_sent\n"
    "legacycid,legacy.com,Legacy,legacy@legacy.com,2026-07-01,False,True\n",
    encoding="utf-8",
)
try:
    config.settings.DATA_DIR = tmp
    e = sent_list.load_entries("legacycid")[0]
    assert e["permanently_excluded"] is False
    assert e["confirmed_sent"] is True and e["contact_email"] == "legacy@legacy.com"
    print("PASS: legacy CSV without the column imports with flag False")
finally:
    db.close()
    config.settings.DATA_DIR = _original_dir
