"""End-to-end login-wall tests via TestClient. Only the Google network
exchange is mocked (auth.handle_login_callback / build_login_url); the
middleware, routes, session cookies, and templates are all real."""
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import app.main as main
import app.auth as auth_mod
from app import config
from app.auth import LoginError

client = TestClient(main.app)

# --- Guard behavior with login required (the live configuration) ---
config.settings.LOGIN_REQUIRED = True

resp = client.get("/", follow_redirects=False)
assert resp.status_code == 303 and resp.headers["location"] == "/login", (resp.status_code, resp.headers.get("location"))
print("PASS: unauthenticated / redirects to /login")

resp = client.post("/sent/516e7c4751/add", data={"contact_email": "x@y.com"}, follow_redirects=False)
assert resp.status_code == 303 and resp.headers["location"] == "/login"
print("PASS: unauthenticated POSTs are blocked too")

resp = client.get("/login")
assert resp.status_code == 200 and "Sign in with Google" in resp.text
print("PASS: /login renders the sign-in page")

# --- Successful login (mock only the Google exchange) ---
auth_mod.handle_login_callback = lambda code, state: {"email": "venkatachengalvala@gmail.com", "name": "Venkata"}
resp = client.get("/auth/callback?code=fake&state=fake", follow_redirects=False)
assert resp.status_code == 303 and resp.headers["location"] == "/"
resp = client.get("/")
assert resp.status_code == 200 and "Venkata" in resp.text and "Sign out" in resp.text
print("PASS: successful callback sets the session; pages load and show the user + Sign out")

# --- Already-logged-in user hitting /login bounces home ---
resp = client.get("/login", follow_redirects=False)
assert resp.status_code == 303 and resp.headers["location"] == "/"
print("PASS: /login redirects home when already signed in")

# --- Logout ---
resp = client.post("/logout", follow_redirects=False)
assert resp.status_code == 303 and resp.headers["location"] == "/login"
resp = client.get("/", follow_redirects=False)
assert resp.status_code == 303 and resp.headers["location"] == "/login"
print("PASS: logout clears the session and the wall is back up")

# --- Rejected login (e.g. allowlist) shows the error on /login ---
def _rejected(code, state):
    raise LoginError("someone@else.com is not authorized to use this app.")
auth_mod.handle_login_callback = _rejected
resp = client.get("/auth/callback?code=fake&state=fake", follow_redirects=True)
assert "not authorized" in resp.text, resp.text[:500]
resp = client.get("/", follow_redirects=False)
assert resp.status_code == 303, "rejected login must not create a session"
print("PASS: rejected login surfaces the error and grants no session")

# --- Provider error / missing code ---
resp = client.get("/auth/callback?error=access_denied", follow_redirects=True)
assert "access_denied" in resp.text
print("PASS: provider errors are shown on the login page")

# --- Login disabled -> app runs open (pre-auth behavior preserved) ---
config.settings.LOGIN_REQUIRED = False
resp = client.get("/", follow_redirects=False)
assert resp.status_code == 200
resp = client.get("/login", follow_redirects=False)
assert resp.status_code == 303 and resp.headers["location"] == "/"
print("PASS: with login disabled, the app behaves exactly as before")
