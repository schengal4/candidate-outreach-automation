"""No draft may go out as one solid block of text: _break_up_wall_of_text
splits any paragraph over 4 sentences into ~3-sentence paragraphs (backstop
for DRAFT_SYSTEM's 2-4 short paragraphs rule — a real run produced an entire
email as a single paragraph)."""
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from app.pipeline import _break_up_wall_of_text

# Condensed from the real single-paragraph draft (Cohere Health run): five
# sentences, no paragraph breaks at all.
wall = (
    "Hi Jack, I saw your post on Chart Chat going live inside Epic and thought it was a smart use of RAG. "
    "That kind of grounded, explainable answer generation is exactly what I've been building at Health Universe. "
    "I also spent time this spring prototyping an AI driven prior authorization system. "
    "I've attached my resume if you want the fuller picture of my background. "
    "Would you be open to a short call sometime to talk about where your engineering team is headed?"
)
fixed = _break_up_wall_of_text(wall)
paragraphs = fixed.split("\n\n")
assert len(paragraphs) == 2, paragraphs
for p in paragraphs:
    assert p.count(". ") + p.count("? ") <= 3, "paragraph still too long: " + repr(p)
print("PASS: a five-sentence wall splits into two readable paragraphs")

# Nothing lost or reordered — same text, just with breaks inserted.
assert fixed.replace("\n\n", " ") == wall
print("PASS: splitting preserves every sentence verbatim")

# A well-formed email (short paragraphs, greeting, closing) passes untouched.
good = (
    "Dear Razik,\n\n"
    "I read the Inside Precision Medicine piece on Atlas. The training set is impressive.\n\n"
    "At Health Universe I led SAM-Med2D. It's used by over 100 clinicians. "
    "The same trust problem seems central to Atlas. I'd value your view on it.\n\n"
    "Thank you for your time,"
)
assert _break_up_wall_of_text(good) == good
print("PASS: an already well-paragraphed email is left byte-for-byte alone")

# Ten sentences in one block -> every resulting paragraph is 4 sentences max.
long_wall = " ".join(f"This is sentence number {i}." for i in range(1, 11))
for p in _break_up_wall_of_text(long_wall).split("\n\n"):
    assert p.count(".") <= 4, p
print("PASS: even a ten-sentence block ends up in digestible paragraphs")
