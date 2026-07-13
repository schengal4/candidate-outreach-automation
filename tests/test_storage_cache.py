"""storage.py on SQLite: round-trips, immediate read-after-write, owner
filtering (normalized case), and concurrent saves from threads don't lose
updates. Uses throwaway candidate ids; real data untouched."""
import sys
import threading
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

import app.storage as storage
from app.models import Candidate

TEMP_IDS = [f"cachetest{i}" for i in range(6)]


def temp_candidate(cid, name, owner="cache.test@example.com"):
    return Candidate(id=cid, name=name, email="", current_employer="X",
                     resume_text="r", owner_email=owner)


try:
    # 1. A save is immediately visible, and the full record round-trips
    storage.save_candidate(temp_candidate(TEMP_IDS[0], "Cache Test 0"))
    got = storage.get_candidate(TEMP_IDS[0])
    assert got.name == "Cache Test 0" and got.owner_email == "cache.test@example.com"
    print("PASS: writes are immediately readable and round-trip")

    # 2. Owner filtering matches case-insensitively (normalized column)
    storage.save_candidate(temp_candidate(TEMP_IDS[1], "Mixed Case", owner="Cache.Test@Example.com"))
    mine = storage.list_candidates(owner_email="cache.test@example.com")
    assert {c.id for c in mine} >= {TEMP_IDS[0], TEMP_IDS[1]}, [c.id for c in mine]
    assert storage.list_candidates(owner_email="nobody@example.com") == []
    print("PASS: owner filtering is case-insensitive and scoped")

    # 3. Updating an existing candidate replaces, not duplicates
    updated = temp_candidate(TEMP_IDS[0], "Renamed")
    storage.save_candidate(updated)
    assert storage.get_candidate(TEMP_IDS[0]).name == "Renamed"
    assert sum(1 for c in storage.list_candidates() if c.id == TEMP_IDS[0]) == 1
    print("PASS: re-saving a candidate updates in place")

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
