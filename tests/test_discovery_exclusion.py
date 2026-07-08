"""Discovery must not resurface companies with an active Sent List entry
(the Rad AI bug). Mocks ask_json; uses a throwaway candidate + sent list."""
import asyncio
import sys
from datetime import date, timedelta
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

import app.pipeline as pipeline
from app import sent_list
from app.models import Candidate, RunState

CID = "discexcltest"
cand = Candidate(id=CID, name="T", email="", current_employer="MyEmployer",
                 resume_text="r", retention_months=12)

captured = {}


async def fake_ask_json(system, user, **kw):
    captured["system"] = system
    captured["user"] = user
    # Model misbehaves and returns excluded companies anyway (with case and
    # www. variations), plus the current employer, plus fresh ones.
    return [
        {"name": "Rad AI", "domain": "WWW.RadAI.com", "reason": "x"},
        {"name": "Viz.ai", "domain": "viz.ai", "reason": "x"},
        {"name": "MyEmployer Inc", "domain": "myemployer.com", "reason": "x"},
        {"name": "FreshCo", "domain": "freshco.com", "reason": "x"},
        {"name": "ExpiredCo", "domain": "expiredco.com", "reason": "x"},
    ]


pipeline.ask_json = fake_ask_json

try:
    # Sent list: active entry (today), permanently-excluded old entry, expired entry
    sent_list.add_entry(CID, "radai.com", "A", "a@radai.com", confirmed_sent=True)
    sent_list.add_entry(CID, "viz.ai", "B", "b@viz.ai",
                        date_sent=(date.today() - timedelta(days=3 * 365)).isoformat(),
                        confirmed_sent=True, permanently_excluded=True)
    sent_list.add_entry(CID, "expiredco.com", "C", "c@expiredco.com",
                        date_sent=(date.today() - timedelta(days=3 * 365)).isoformat(),
                        confirmed_sent=True)

    run = RunState(id="dr1", candidate_id=CID)
    asyncio.run(pipeline.run_discovery(run, cand))

    domains = [c["domain"] for c in run.discovered]
    assert "radai.com" not in domains, "active sent-list company must be filtered (even as WWW.RadAI.com)"
    assert "viz.ai" not in domains, "permanently-excluded company must be filtered regardless of age"
    assert "myemployer.com" not in domains, "employer exclusion unchanged"
    assert "freshco.com" in domains, "fresh companies still pass"
    assert "expiredco.com" in domains, "entries past the retention window must NOT block the company"
    print("PASS: hard filter drops active + permanent sent-list companies, keeps fresh + expired ones")

    assert "Already-contacted companies (do NOT include any of these)" in captured["user"]
    assert "radai.com" in captured["user"] and "viz.ai" in captured["user"]
    assert "expiredco.com" not in captured["user"].split("Already-contacted")[1], \
        "expired entries must not be in the prompt exclusion list"
    assert "NEVER include any company on the already-contacted list" in captured["system"]
    print("PASS: prompt tells the model up front which companies to skip (active ones only)")

    # No sent list at all -> no exclusion block, nothing filtered
    sent_list.delete_list(CID)
    run2 = RunState(id="dr2", candidate_id=CID)
    asyncio.run(pipeline.run_discovery(run2, cand))
    assert "Already-contacted" not in captured["user"]
    assert "radai.com" in [c["domain"] for c in run2.discovered]
    print("PASS: with an empty sent list, discovery behaves exactly as before")
finally:
    sent_list.delete_list(CID)
