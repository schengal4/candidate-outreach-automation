"""Verify permanently_excluded overrides the retention window and pruning,
using a throwaway candidate id so real CSVs are untouched."""
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from app import sent_list

CID = "testpermflag"
path = sent_list._path(CID)
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

    # Toggle off -> the same old entry now expires like any other
    sent_list.update_entry(CID, 0, permanently_excluded=False)
    assert "never@never.com" not in sent_list.active_blocked_emails(CID, 12)
    print("PASS: toggling the flag off restores normal retention behaviour")

    # Old CSVs without the column parse as False
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write("candidate_id,company_domain,contact_name,contact_email,date_sent,interview_arranged,confirmed_sent\n")
        f.write(f"{CID},legacy.com,Legacy,legacy@legacy.com,2026-07-01,False,True\n")
    e = sent_list.load_entries(CID)[0]
    assert e["permanently_excluded"] is False
    print("PASS: legacy CSV without the column loads with flag False")
finally:
    if path.exists():
        path.unlink()
        print("cleanup: removed", path.name)
