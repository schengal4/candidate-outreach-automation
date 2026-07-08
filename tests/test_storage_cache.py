"""storage.py cache + lock: repeated reads don't re-parse, external file
changes are picked up, saves are immediately visible, and concurrent saves
don't lose updates. Uses throwaway candidate ids; real data untouched."""
import json
import sys
import threading
import time
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

import app.storage as storage
from app.models import Candidate

# Count actual JSON parses by wrapping json.load as seen from storage.py
parse_count = {"n": 0}
_orig_load = storage.json.load


def counting_load(f):
    parse_count["n"] += 1
    return _orig_load(f)


storage.json = type(sys)("json_proxy")
storage.json.load = counting_load
storage.json.dump = json.dump
storage.json.dumps = json.dumps  # used by the atomic-write save path

TEMP_IDS = [f"cachetest{i}" for i in range(6)]


def temp_candidate(cid, name):
    return Candidate(id=cid, name=name, email="", current_employer="X",
                     resume_text="r", owner_email="cache.test@example.com")


try:
    # 1. Repeated reads hit the cache (at most one parse)
    storage._cache = None  # start cold
    parse_count["n"] = 0
    for _ in range(50):
        storage.list_candidates()
        storage.get_candidate("516e7c4751")
    assert parse_count["n"] == 1, f"expected 1 parse for 100 reads, got {parse_count['n']}"
    print("PASS: 100 consecutive reads = 1 JSON parse (cache hit)")

    # 2. A save is immediately visible without stale reads
    storage.save_candidate(temp_candidate(TEMP_IDS[0], "Cache Test 0"))
    assert storage.get_candidate(TEMP_IDS[0]).name == "Cache Test 0"
    print("PASS: writes are immediately readable")

    # 3. External modification (another process editing the file) is noticed
    time.sleep(0.02)  # ensure mtime_ns advances even on coarse clocks
    raw = json.loads(storage.CANDIDATES_FILE.read_text(encoding="utf-8"))
    raw[TEMP_IDS[1]] = temp_candidate(TEMP_IDS[1], "External Edit").to_dict()
    storage.CANDIDATES_FILE.write_text(json.dumps(raw), encoding="utf-8")
    assert storage.get_candidate(TEMP_IDS[1]).name == "External Edit"
    print("PASS: external file edits invalidate the cache")

    # 4. Concurrent saves from threads don't drop each other (the old race)
    def save_from_thread(i):
        storage.save_candidate(temp_candidate(TEMP_IDS[2 + i], f"Thread {i}"))

    threads = [threading.Thread(target=save_from_thread, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for i in range(4):
        assert storage.get_candidate(TEMP_IDS[2 + i]) is not None, f"lost thread {i}'s save"
    print("PASS: 4 concurrent saves all persisted (no lost updates)")
finally:
    for cid in TEMP_IDS:
        storage.delete_candidate(cid)
    leftovers = [c.id for c in storage.list_candidates() if c.id.startswith("cachetest")]
    assert not leftovers, leftovers
    print("cleanup: all temp candidates removed")
