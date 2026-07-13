"""Unit tests for app/auth.py internals: the state->verifier bridge,
ID-token claim checks, and the allowlist — with Google's Flow and token
verifier patched out."""
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

import app.auth as auth
from app import config
from app.auth import LoginError


class FakeCreds:
    id_token = "fake-jwt"


class FakeFlow:
    code_verifier = "fake-verifier"
    credentials = FakeCreds()

    @classmethod
    def from_client_config(cls, *a, **kw):
        inst = cls()
        inst.kwargs = kw
        return inst

    def authorization_url(self, **kw):
        self._state = kw.get("state")
        return (f"https://accounts.google.com/o/oauth2/auth?state={self._state}", self._state)

    def fetch_token(self, code):
        assert self.kwargs.get("code_verifier") == "fake-verifier", "verifier not bridged"


auth.Flow = FakeFlow

CLAIMS = {}
auth.google_id_token.verify_oauth2_token = lambda tok, req, aud, clock_skew_in_seconds=0: dict(CLAIMS)


def state_from(url):
    return url.rsplit("state=", 1)[1]


# 1. Happy path: verifier bridged by state, verified email returned lowercased
CLAIMS.update({"email": "Venkatachengalvala@Gmail.com", "email_verified": True, "name": "Venkata"})
url = auth.build_login_url()
user = auth.handle_login_callback("code", state_from(url))
assert user == {"email": "venkatachengalvala@gmail.com", "name": "Venkata"}, user
print("PASS: happy path — verifier bridged via state, email normalized")

# 2. Unknown/reused state is rejected (anti-CSRF + restart recovery)
try:
    auth.handle_login_callback("code", state_from(url))  # state was popped in test 1
    assert False
except LoginError as e:
    assert "try again" in str(e).lower()
print("PASS: reused/unknown state is rejected")

# 3. Unverified email rejected
CLAIMS.update({"email": "x@y.com", "email_verified": False})
try:
    auth.handle_login_callback("code", state_from(auth.build_login_url()))
    assert False
except LoginError as e:
    assert "verified" in str(e)
print("PASS: unverified email is rejected")

# 4. Allowlist enforced
config.settings.ALLOWED_LOGIN_EMAILS = {"venkatachengalvala@gmail.com"}
CLAIMS.update({"email": "intruder@example.com", "email_verified": True})
try:
    auth.handle_login_callback("code", state_from(auth.build_login_url()))
    assert False
except LoginError as e:
    assert "not authorized" in str(e)
CLAIMS.update({"email": "venkatachengalvala@gmail.com"})
user = auth.handle_login_callback("code", state_from(auth.build_login_url()))
assert user["email"] == "venkatachengalvala@gmail.com"
print("PASS: allowlist blocks other emails, admits listed ones")
