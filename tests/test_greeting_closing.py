"""Every generated draft must open by addressing the contact and end with a
closing line ("Best," etc.) ahead of the auto-appended signature — real runs
produced drafts missing one or both, so draft_email enforces them in code
(_ensure_greeting_and_closing) on top of the DRAFT_SYSTEM prompt rules."""
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from app.pipeline import _ensure_greeting_and_closing

# Model omitted both (seen in a real run: the PathAI draft opened mid-thought
# and ended on the ask with no sign-off).
body = "I saw your post about ArtifactDetect.\n\nWould you be open to a chat?"
fixed = _ensure_greeting_and_closing(body, "Nishant")
assert fixed.startswith("Hi Nishant,\n\n"), fixed[:40]
assert fixed.endswith("Would you be open to a chat?\n\nBest,"), fixed[-60:]
print("PASS: missing greeting and closing are both added")

# Model already wrote both — the body must pass through untouched.
body = "Dear Razik,\n\nGreat to see the Atlas work.\n\nThank you for your time,"
assert _ensure_greeting_and_closing(body, "Razik") == body
print("PASS: existing greeting and closing are left alone")

# Inline greeting ("Hi Jack, I saw...") counts as a greeting.
body = "Hi Jack, I saw your post on Chart Chat.\n\nWould you be open to a call?"
fixed = _ensure_greeting_and_closing(body, "Jack")
assert fixed.startswith("Hi Jack, I saw"), fixed[:40]
assert fixed.endswith("\n\nBest,"), fixed[-20:]
print("PASS: inline greeting recognized; only the closing is added")

# No first name available -> don't guess a greeting; closing still enforced.
fixed = _ensure_greeting_and_closing("Quick note about your team.", "")
assert fixed == "Quick note about your team.\n\nBest,"
print("PASS: without a first name only the closing is added")

# A contact whose name happens to start a sentence-like body must not be
# mistaken for a greeting ("Great work..." does not start with hi/hello/dear).
fixed = _ensure_greeting_and_closing("Great work on the launch.\n\nBest,", "Enhao")
assert fixed.startswith("Hi Enhao,\n\nGreat work"), fixed[:40]
assert fixed.count("Best,") == 1, "closing must not be duplicated"
print("PASS: existing closing is not duplicated when only the greeting is added")
