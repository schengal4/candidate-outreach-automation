"""Answering the user's question directly: simulate run 1 adding drafts,
confirming some as sent, then run 2 adding 10 more and confirming all —
verify nothing from run 1 is lost and all flags/dates survive. Uses a
throwaway candidate id; runs through the real routes where possible."""
import sys
from datetime import date
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import app.main as main
import app.auth as auth_mod
from app import config, sent_list, storage
from app.models import Candidate

CID = "cumultest01"
EMAIL = "cumul.test@example.com"

config.settings.LOGIN_REQUIRED = True
storage.save_candidate(Candidate(id=CID, name="Cumul Test", email="", current_employer="X",
                                 resume_text="r", owner_email=EMAIL))
client = TestClient(main.app)
auth_mod.handle_login_callback = lambda code, state: {"email": EMAIL, "name": "T"}
client.get("/auth/callback?code=x&state=y")

try:
    # --- "Run 1": pipeline auto-adds 3 drafts (confirmed_sent=False, today) ---
    for i in range(3):
        sent_list.add_entry(CID, f"run1co{i}.com", f"Contact {i}", f"c{i}@run1co{i}.com")
    assert len(sent_list.load_entries(CID)) == 3

    # User confirms 2 of them via the real route, sets Never-contact on one
    # (entries are addressed by their stable DB id, not by list position)
    ids = [e["id"] for e in sent_list.load_entries(CID)]
    client.post(f"/sent/{CID}/{ids[0]}/update", data={"action": "confirm_sent"})
    client.post(f"/sent/{CID}/{ids[1]}/update", data={"action": "confirm_sent"})
    client.post(f"/sent/{CID}/{ids[1]}/update", data={"action": "toggle_permanent"})
    entries = sent_list.load_entries(CID)
    assert entries[0]["confirmed_sent"] and entries[1]["confirmed_sent"] and not entries[2]["confirmed_sent"]
    assert entries[1]["permanently_excluded"]
    print("PASS: run 1 - 3 auto-added, 2 confirmed via the UI route, 1 marked never-contact")

    # --- "Run 2": pipeline auto-adds 10 more ---
    for i in range(10):
        sent_list.add_entry(CID, f"run2co{i}.com", f"New {i}", f"n{i}@run2co{i}.com")
    entries = sent_list.load_entries(CID)
    assert len(entries) == 13, f"expected 13 cumulative entries, got {len(entries)}"
    # Run-1 entries and their flags are untouched by run-2 appends
    assert entries[0]["company_domain"] == "run1co0.com" and entries[0]["confirmed_sent"]
    assert entries[1]["permanently_excluded"] and entries[1]["confirmed_sent"]
    assert not entries[2]["confirmed_sent"]
    assert entries[0]["date_sent"] == date.today().isoformat()
    print("PASS: run 2 appended 10 -> 13 total; run-1 entries, flags, and dates all preserved")

    # --- User clicks "Yes — I sent all of these" (confirm_all route) ---
    client.post(f"/sent/{CID}/confirm_all")
    entries = sent_list.load_entries(CID)
    assert len(entries) == 13 and all(e["confirmed_sent"] for e in entries)
    assert entries[1]["permanently_excluded"], "confirm_all must not clear other flags"
    print("PASS: confirm-all marks everything sent without dropping or altering entries")

    # --- All 13 block re-contact, and all 13 companies are excluded from discovery ---
    blocked = sent_list.active_blocked_emails(CID, 12)
    assert len(blocked) == 13
    active_domains = {e["company_domain"] for e in sent_list.active_entries(CID, 12)}
    assert len(active_domains) == 13
    print("PASS: all 13 cumulative entries actively block contacts AND their companies in discovery")

    # --- Prune (the only remover besides manual delete) touches nothing current ---
    removed = sent_list.prune_expired(CID, 12)
    assert removed == 0 and len(sent_list.load_entries(CID)) == 13
    print("PASS: prune removes nothing that's still inside the retention window")
finally:
    sent_list.delete_list(CID)
    storage.delete_candidate(CID)
    print("cleanup: temp candidate + sent list removed")
