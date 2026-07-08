"""Atomic file writes: content lands correctly, a crash mid-write leaves the
original file untouched, and the temp file never lingers."""
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

import tempfile
from pathlib import Path

import app.fsutil as fsutil
from app import sent_list

with tempfile.TemporaryDirectory() as td:
    target = Path(td) / "victim.json"

    # 1. Plain write works
    fsutil.atomic_write_text(target, '{"v": 1}')
    assert target.read_text(encoding="utf-8") == '{"v": 1}'
    print("PASS: atomic_write_text writes content")

    # 2. Simulated crash during the final swap: original survives, temp cleaned
    orig_replace = fsutil.os.replace
    fsutil.os.replace = lambda a, b: (_ for _ in ()).throw(OSError("simulated crash"))
    try:
        try:
            fsutil.atomic_write_text(target, '{"v": 2, "corrupt": tru')
            assert False, "expected the simulated crash to propagate"
        except OSError:
            pass
    finally:
        fsutil.os.replace = orig_replace
    assert target.read_text(encoding="utf-8") == '{"v": 1}', "original must be untouched"
    assert list(Path(td).glob("*.tmp")) == [], "temp file must be cleaned up"
    print("PASS: failed write leaves the original intact and no temp litter")

    # 3. Bytes variant
    fsutil.atomic_write_bytes(target, b"\x00\x01binary")
    assert target.read_bytes() == b"\x00\x01binary"
    print("PASS: atomic_write_bytes round-trips binary content")

# 4. Sent list CSV round-trips identically through the new writer
CID = "atomictest01"
try:
    sent_list.add_entry(CID, "a.com", "A B", "a@a.com", confirmed_sent=True)
    sent_list.add_entry(CID, "b.com", "C D", "c@b.com", permanently_excluded=True)
    entries = sent_list.load_entries(CID)
    assert len(entries) == 2
    assert entries[0]["confirmed_sent"] is True
    assert entries[1]["permanently_excluded"] is True
    print("PASS: sent-list CSV round-trips through the atomic writer")
finally:
    sent_list.delete_list(CID)
