"""Verify the "No — I didn't send these" answer to the reconciliation nudge:
POST /sent/{cid}/dismiss_pending deletes exactly the unconfirmed entries the
nudge listed, so contacts become reachable again — while confirmed entries
and unlisted pending ones (e.g. added by a concurrently running pipeline)
survive. Uses a throwaway candidate so real data is untouched."""
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from app import config, sent_list, storage
from app.main import app
from app.models import Candidate

config.settings.LOGIN_REQUIRED = False

CID = "testdismisspend"
client = TestClient(app)
try:
    storage.save_candidate(Candidate(id=CID, name="Smoke Test", email="s@s.com",
                                     current_employer="X", resume_text="r"))

    id_a = sent_list.add_entry(CID, "a.com", "A", "a@a.com")   # pending, listed
    id_b = sent_list.add_entry(CID, "b.com", "B", "b@b.com")   # pending, listed
    id_c = sent_list.add_entry(CID, "c.com", "C", "c@c.com",
                               confirmed_sent=True)             # confirmed
    id_d = sent_list.add_entry(CID, "d.com", "D", "d@d.com")   # pending, not listed
                                                                # (concurrent run)

    html = client.get(f"/candidates/{CID}").text
    assert "No — I didn't send these" in html
    assert f'name="entry_ids" value="{id_a}"' in html
    print("PASS: nudge renders the No button with hidden entry ids")

    resp = client.post(
        f"/sent/{CID}/dismiss_pending",
        data={"entry_ids": [str(id_a), str(id_b)]},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    remaining = {e["id"] for e in sent_list.load_entries(CID)}
    assert remaining == {id_c, id_d}, f"unexpected remaining ids: {remaining}"
    print("PASS: listed pending entries deleted; confirmed + unlisted survive")

    # A confirmed entry passed in the form must not be deleted.
    client.post(f"/sent/{CID}/dismiss_pending",
                data={"entry_ids": [str(id_c)]}, follow_redirects=False)
    assert id_c in {e["id"] for e in sent_list.load_entries(CID)}
    print("PASS: confirmed entries can't be deleted via dismiss_pending")

    # Submitting with no ids is a harmless no-op.
    resp = client.post(f"/sent/{CID}/dismiss_pending", data={}, follow_redirects=False)
    assert resp.status_code == 303
    assert {e["id"] for e in sent_list.load_entries(CID)} == {id_c, id_d}
    print("PASS: empty submission is a no-op")
finally:
    sent_list.delete_list(CID)
    storage.delete_candidate(CID)
    print("cleanup: removed throwaway candidate and sent list")
