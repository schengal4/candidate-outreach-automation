"""extract_json tolerance: prose around a value, fenced blocks, and — the
regression a real run hit — two concatenated top-level JSON objects (a false
start '{"summary": "hold","items":[]}' glued to the real research payload).
The old first-{-to-last-} slice spanned both objects and failed the parse,
dropping a company whose full research was sitting in the reply."""
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from app.llm import LLMError, extract_json

# 1. Plain object / whole reply
assert extract_json('{"a": 1}') == {"a": 1}
print("PASS: whole-reply JSON parses")

# 2. Fenced code block still wins over surrounding prose
assert extract_json('Here you go:\n```json\n{"a": 2}\n```\nDone.') == {"a": 2}
print("PASS: fenced block parses")

# 3. Prose around a single object
assert extract_json('Result: {"a": 3} — hope that helps!') == {"a": 3}
print("PASS: prose around one object parses")

# 4. THE regression: false-start object concatenated with the real payload —
#    the meatier value wins, in either order
false_start = '{"summary": "hold","items":[],"red_flags":[]}'
real = ('{"summary": "Joe DeVivo is CEO of Butterfly Network.", '
        '"items": [{"fact": "Q1 2026 earnings beat", "source": "BW", '
        '"date": "2026-04-30", "url": "https://x.com"}], "red_flags": []}')
parsed = extract_json(false_start + real)
assert parsed["summary"].startswith("Joe DeVivo"), parsed
assert parsed["items"], parsed
parsed = extract_json(real + false_start)
assert parsed["summary"].startswith("Joe DeVivo"), parsed
print("PASS: concatenated false start + real payload -> real payload wins")

# 5. Concatenated values with prose between them
parsed = extract_json(f"first attempt:\n{false_start}\nactually, here:\n{real}")
assert parsed["items"], parsed
print("PASS: multiple values separated by prose -> meatiest wins")

# 6. Arrays still work, including trailing prose
assert extract_json('[1, 2, 3] is the list') == [1, 2, 3]
print("PASS: top-level array parses")

# 7. Stray braces before the real value don't derail the scan
assert extract_json('weird {not json} but {"a": 4} works') == {"a": 4}
print("PASS: invalid brace runs are skipped, valid value found")

# 8. No JSON at all still raises LLMError
try:
    extract_json("Sorry, I cannot help with that.")
    raise AssertionError("expected LLMError")
except LLMError:
    pass
print("PASS: reply with no JSON raises LLMError")
