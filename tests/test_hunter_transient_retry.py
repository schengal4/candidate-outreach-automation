"""Hunter transient-network-error handling: one retry on a requests network
failure (a real run hit an SSL EOF mid-handshake and dropped a fully-verified
company), then a persistent failure degrades to (None, None, "") — the
"no email found" answer — so the pipeline drafts for manual outreach instead
of dropping. Real Hunter API errors (bad key, out of credits) still don't
retry."""
import asyncio
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

import requests

import app.hunter_async as hunter_async
from hunter_client import HunterAPIError

hunter_async.TRANSIENT_RETRY_DELAY_SECONDS = 0  # no real sleeping in tests

FOUND = {"data": {"email": "jane@x.com", "score": 95, "linkedin_url": "https://linkedin.com/in/jane"}}


class FakeClient:
    def __init__(self, outcomes):
        self.seq = list(outcomes)
        self.calls = 0

    def email_finder(self, domain, first_name, last_name):
        self.calls += 1
        outcome = self.seq.pop(0)
        if outcome == "ssl":
            raise requests.exceptions.SSLError("EOF occurred in violation of protocol")
        if outcome == "api":
            raise HunterAPIError(401, [{"details": "bad key"}])
        return FOUND


def run_with(outcomes):
    client = FakeClient(outcomes)
    hunter_async._client = client
    result = asyncio.run(hunter_async.find_email("x.com", "Jane", "Doe"))
    return client, result


real_client = hunter_async._client
try:
    # 1. One SSL blip -> retried, email comes back
    client, (email, score, linkedin) = run_with(["ssl", "ok"])
    assert client.calls == 2, client.calls
    assert email == "jane@x.com" and score == 95 and "linkedin.com/in/jane" in linkedin
    print("PASS: one transient network error is retried and the lookup succeeds")

    # 2. Persistent network failure -> (None, None, "") after exactly 2 tries,
    #    never an exception (the pipeline treats it as "no email found")
    client, result = run_with(["ssl", "ssl"])
    assert client.calls == 2, client.calls
    assert result == (None, None, ""), result
    print("PASS: a persistent network error degrades to 'no email found'")

    # 3. Hunter API errors are a verdict, not a blip — no retry
    client, result = run_with(["api"])
    assert client.calls == 1, client.calls
    assert result == (None, None, ""), result
    print("PASS: Hunter API errors are not retried")
finally:
    hunter_async._client = real_client
