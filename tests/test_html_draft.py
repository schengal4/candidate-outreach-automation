"""Gmail drafts must carry an HTML alternative next to the plain text.

A text/plain-only draft puts Gmail's composer in plain-text mode, and Gmail
hard-wraps plain text at ~70 chars on SEND — recipients saw a narrow ragged
column (confirmed against a real sent message). The HTML part makes Gmail
compose/send rich text that reflows. Verifies the full MIME shape with an
attachment: multipart/mixed [ multipart/alternative [plain, html], pdf ]."""
import base64
import sys
from email import message_from_bytes
from email.policy import default
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from app.gmail_client import _build_raw_message, _body_to_html

body = (
    "Hi Brendan,\n\n"
    "Your August post about the <demo> gap & production AI stuck around in my head. "
    "This paragraph is deliberately much longer than seventy characters to prove nothing wraps it.\n\n"
    "Best,\n\n"
    "--\nVenkata Chengalvala\nvenkatachengalvala@gmail.com"
)

raw = _build_raw_message(
    "brendan@example.com", "Subject", body,
    attachment=b"%PDF-1.4 fake", attachment_name="resume.pdf",
)
msg = message_from_bytes(base64.urlsafe_b64decode(raw), policy=default)

# Overall shape: mixed(alternative(plain, html), pdf)
assert msg.get_content_type() == "multipart/mixed", msg.get_content_type()
parts = list(msg.iter_parts())
assert parts[0].get_content_type() == "multipart/alternative", parts[0].get_content_type()
alt = list(parts[0].iter_parts())
assert [p.get_content_type() for p in alt] == ["text/plain", "text/html"], alt
assert parts[1].get_content_type() == "application/pdf"
assert parts[1].get_filename() == "resume.pdf"
print("PASS: draft is multipart/mixed with a plain+html alternative and the PDF")

# Plain part unchanged — long paragraphs stay on one line.
plain_text = msg.get_body(preferencelist=("plain",)).get_content()
assert plain_text.strip() == body.strip(), "plain part altered"
print("PASS: plain-text part still carries the exact unwrapped body")

# HTML part: paragraphs as <p>, signature line breaks as <br>, HTML escaped.
html_text = msg.get_body(preferencelist=("html",)).get_content()
assert "&lt;demo&gt; gap &amp; production" in html_text, "HTML must escape < > &"
assert html_text.count("<p>") == 4, html_text
assert "--<br>Venkata Chengalvala<br>" in html_text, "signature keeps its line breaks"
assert "style" not in html_text.lower(), "must stay unstyled — it's a personal email"
print("PASS: HTML part escapes content, keeps paragraphs and signature breaks, no styling")

# No hard-wrapping sneaks into the HTML text either.
assert "seventy characters to prove nothing wraps it" in html_text.replace("\n", "")
print("PASS: long paragraphs reach the HTML part intact")

# Helper sanity: empty-ish body doesn't blow up.
assert _body_to_html("One line.") == "<html><body><p>One line.</p></body></html>"
print("PASS: single-paragraph body converts cleanly")
