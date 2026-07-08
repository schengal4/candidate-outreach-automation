"""Verify _unwrap_paragraphs fixes hard-wrapped model output, matching the
shape of the screenshot (each paragraph pre-wrapped at ~70 chars, blank line
between paragraphs), and that it survives the actual Gmail MIME round-trip."""
import base64
import sys
from email import message_from_bytes
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from app.pipeline import _unwrap_paragraphs
from app.gmail_client import _build_raw_message

wrapped = (
    "Dear Elad,\n\n"
    "Congrats on First Read getting its Breakthrough Device Designation.\n"
    "Drafting radiology reports straight from chest X-rays is a hard trust\n"
    "problem to solve, let alone clear with the FDA twice in under a year.\n\n"
    "I've spent the last two years building clinical imaging AI at Health\n"
    "Universe, including SAM-Med2D, a segmentation tool now used by over\n"
    "100 clinicians and researchers for CT and MRI workflows.\n\n"
    "Would you have 15 minutes in the next couple weeks?"
)

unwrapped = _unwrap_paragraphs(wrapped)
print(repr(unwrapped))

paragraphs = unwrapped.split("\n\n")
assert len(paragraphs) == 4, paragraphs
assert "\n" not in paragraphs[1], "paragraph should be a single line: " + repr(paragraphs[1])
assert paragraphs[1] == (
    "Congrats on First Read getting its Breakthrough Device Designation. "
    "Drafting radiology reports straight from chest X-rays is a hard trust "
    "problem to solve, let alone clear with the FDA twice in under a year."
)
print("PASS: hard-wrapped paragraphs collapse to single lines; blank-line breaks preserved")

# Round-trip through the real MIME builder used for Gmail drafts.
raw = _build_raw_message("elad@example.com", "Subject", unwrapped)
msg = message_from_bytes(base64.urlsafe_b64decode(raw))
body_text = msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8")
assert body_text.strip() == unwrapped.strip(), "MIME round-trip altered the body text"
print("PASS: unwrapped body survives the Gmail MIME message round-trip unchanged")
