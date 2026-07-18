"""Deterministic post-processing of model output: draft finishing and the
banned-style backstop.

Everything here is pure text-in/text-out — no LLM calls, no state. It exists
because prompt rules alone don't hold: real runs leaked banned phrasing,
opened mid-thought, hard-wrapped paragraphs, and shipped walls of text
straight past explicit prompt instructions, so each rule the report relies
on also has an enforcement or detection pass here.
"""

import re
from typing import List

from .models import Candidate

# Web-search replies sometimes leak literal <cite index="..."> markup into the
# JSON strings; it renders raw in the run report and could bleed into a draft.
_CITE_TAG_RE = re.compile(r"</?cite[^>]*>")


def strip_cite_tags(text: str) -> str:
    return _CITE_TAG_RE.sub("", text).strip()


# Belt and suspenders on top of DRAFT_SYSTEM's banned-wording list: real runs
# leaked "caught my attention" (three times in one run), "stuck with me", and
# "see if there's a fit" straight past the prompt rule, so — like the
# greeting/closing guarantee below — enforcement can't live in the prompt
# alone. A hit is flagged on the draft result (see steps.draft_email) so the
# run report asks the user to fix the wording; phrases here should be
# high-signal AI/cold-outreach tells, not words a good email might need.
_BANNED_STYLE_RE = re.compile(
    "|".join(
        [
            r"—",  # em dash
            r"\b(?:delve|delving|moreover|furthermore|albeit|indeed|certainly"
            r"|underscores?|pivotal|realm|harness(?:es|ing)?|illuminates?"
            r"|facilitates?|bolster(?:s|ing)?|streamlin(?:es?|ing)"
            r"|revolutioniz\w+|innovative|cutting-edge|game-changing"
            r"|transformative|seamless(?:ly)?|leverag(?:es?|ing)|robust)\b",
            r"shed(?:s|ding)? light on",
            r"at its core",
            r"that being said",
            r"a testament to",
            r"a (?:tapestry|symphony) of",
            r"nestled in",
            r"stuck with me",
            r"struck me",
            r"resonated? with me",
            r"caught my (?:eye|attention)",
            r"matches (?:almost )?exactly what",
            r"aligns closely with",
            r"seems? central to what",
            r"i'?d welcome a short conversation",
            r"there'?s a fit",
            r"i'?ve been following your work",
            r"\bi offer\b",  # resume-speak: "I offer three years of experience"
            # The curiosity-dressed ask ("I'd like to hear how your team
            # thinks about X") — the recipient knows it's a job inquiry.
            r"i'?d (?:like|love) to hear (?:how|more about|where)",
            r"from one \w+ to another",
        ]
    ),
    re.IGNORECASE,
)


def banned_style_hits(text: str) -> List[str]:
    """Distinct banned words/phrases present in text (lowercased, sorted)."""
    return sorted({m.group(0).lower() for m in _BANNED_STYLE_RE.finditer(text)})


def unwrap_paragraphs(text: str) -> str:
    """Collapse single line breaks within a paragraph into spaces, keeping
    blank-line paragraph breaks intact.

    The model sometimes hard-wraps prose at a fixed column width inside the
    JSON "body" string. A plain-text email (like Gmail drafts) renders every
    line break literally, so that wrapping survives as a narrow, ragged
    column that doesn't fill the reading pane, instead of reflowing prose.
    """
    paragraphs = text.split("\n\n")
    return "\n\n".join(
        " ".join(line.strip() for line in p.splitlines() if line.strip())
        for p in paragraphs
    )


_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+")


def break_up_wall_of_text(body: str, max_sentences: int = 4) -> str:
    """Backstop for DRAFT_SYSTEM's paragraph rule: split any paragraph longer
    than max_sentences into roughly equal chunks of whole sentences. Real runs
    occasionally produced the entire email as one solid block, which is the
    single fastest way to get a cold email closed unread."""
    out = []
    for p in body.split("\n\n"):
        sentences = _SENTENCE_END_RE.split(p.strip())
        if len(sentences) <= max_sentences:
            out.append(p)
            continue
        n_chunks = -(-len(sentences) // 3)  # ceil: aim for ~3 sentences each
        size = -(-len(sentences) // n_chunks)
        out.extend(
            " ".join(sentences[i : i + size]) for i in range(0, len(sentences), size)
        )
    return "\n\n".join(out)


# Belt and suspenders on top of DRAFT_SYSTEM's greeting/closing rules: real
# runs produced drafts that opened mid-thought or ended with no sign-off, so
# the guarantee can't live in the prompt alone.
_GREETING_RE = re.compile(r"^(hi|hello|hey|dear)\b", re.IGNORECASE)

# A final line that is really a question squeezed into the closing-comma
# format ("Would you be open to a short call sometime,") — the format rule
# demands the body end in a comma, so models comply by mangling their own
# question, and a real recipient reads it as a typo. Interrogative opener +
# a you/we somewhere + trailing comma; plain closings ("Best,", "Would love
# to talk,") don't match.
_QUESTION_AS_CLOSING_RE = re.compile(
    r"^(?:would|could|can|will|do|does|are|is|should|may|might)\b.*\b(?:you|we)\b.*,$",
    re.IGNORECASE,
)


def ensure_greeting_and_closing(body: str, first_name: str) -> str:
    """Guarantee the email opens by addressing the contact and ends with a
    closing line ("Best," etc.) ahead of the auto-appended signature. A final
    question gets its question mark back and a real closing after it."""
    body = body.strip()
    if not body:
        return body
    if first_name and not _GREETING_RE.match(body):
        body = f"Hi {first_name},\n\n{body}"
    lines = body.splitlines()
    if _QUESTION_AS_CLOSING_RE.match(lines[-1].strip()):
        lines[-1] = lines[-1].rstrip().rstrip(",") + "?"
        body = "\n".join(lines)
    if not body.splitlines()[-1].strip().endswith(","):
        body += "\n\nBest,"
    return body


def finalize_body(raw_body: str, first_name: str) -> str:
    """The full finishing chain every draft body (first drafts and claim
    revisions alike) goes through before the signature is appended."""
    body = unwrap_paragraphs(raw_body.strip())
    body = break_up_wall_of_text(body)
    return ensure_greeting_and_closing(body, first_name)


# Separator between the drafted body and the auto-appended sign-off; also how
# the claim-revision step strips the sign-off back off before showing the
# draft to the model.
SIGNATURE_SEP = "\n\n--\n"


def email_signature(candidate: Candidate) -> str:
    """A plain sign-off appended to every draft: name, email, LinkedIn if given —
    like a normal email, not bulk-mail boilerplate. Not editable by the candidate."""
    lines = [candidate.name]
    if candidate.email:
        lines.append(candidate.email)
    if candidate.linkedin_url:
        lines.append(candidate.linkedin_url)
    return SIGNATURE_SEP + "\n".join(lines)
