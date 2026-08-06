#!/usr/bin/env python3
"""
Regression test for the dead-letter fix to Fix 5's write-ahead recovery
queue (this session): a write that fails identically forever (e.g. its
payload predates a field the API now requires) used to retry and fail on
every single future run, with the pending queue only ever growing. Now
`rewrite_pending_writes()` tracks an `attempts` counter per entry and, on
reaching `MAX_WRITE_ATTEMPTS`, moves it to `DEAD_LETTER_PATH` with its
failure reason instead of writing it back to the pending file.

Isolated from both real queue files the same way test_fail_loud_
persistence.py is isolated from the real pending-writes queue --
fe.PENDING_WRITES_PATH and fe.DEAD_LETTER_PATH are both monkeypatched to a
tempdir for this test's duration, restored in a finally, and the real
files' content hashes are asserted unchanged before/after.

Run: python3 scripts/tests/test_dead_letter_queue.py
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
sys.path.insert(0, SCRIPTS)
sys.path.insert(0, os.path.join(os.path.dirname(SCRIPTS), "engines"))

import forecast_engine as fe  # noqa: E402

TEST_PAYLOAD = {
    "ticker": "TEST", "trading_date": "2026-08-06", "as_of_ts": "2026-08-06T12:00:00+00:00",
    "horizon_days": 5, "model_version": "test-model", "voter": "forecast",
}

REAL_PENDING_WRITES_PATH = fe.PENDING_WRITES_PATH
REAL_DEAD_LETTER_PATH = fe.DEAD_LETTER_PATH


def _hash(path):
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def reset_state():
    fe._run_write_outcomes.clear()
    fe._run_write_failures.clear()
    for p in (fe.PENDING_WRITES_PATH, fe.DEAD_LETTER_PATH):
        if os.path.exists(p):
            os.remove(p)


def run_tests():
    ok = True
    reset_state()

    # 1. A write that fails MAX_WRITE_ATTEMPTS times across separate
    #    "runs" (each run = a fresh flush+attempt+rewrite cycle, matching
    #    main()'s own shape) gets dead-lettered on the Nth failure, not
    #    before.
    fe.mm_journal = lambda op, payload: None  # every attempt fails
    write_id = None
    for attempt in range(1, fe.MAX_WRITE_ATTEMPTS + 1):
        fe._run_write_outcomes.clear()
        fe._run_write_failures.clear()
        if attempt == 1:
            try:
                fe.persist_or_raise("create_forecast", TEST_PAYLOAD)
            except fe.PersistenceError:
                pass
            with open(fe.PENDING_WRITES_PATH) as f:
                write_id = json.loads(f.readline())["write_id"]
        else:
            fe.flush_pending_writes()
        fe.rewrite_pending_writes()

        if attempt < fe.MAX_WRITE_ATTEMPTS:
            if os.path.exists(fe.DEAD_LETTER_PATH):
                print(f"FAIL: dead-lettered after only {attempt} attempts "
                      f"(expected {fe.MAX_WRITE_ATTEMPTS})")
                ok = False
                break
            with open(fe.PENDING_WRITES_PATH) as f:
                entry = json.loads(f.readline())
            if entry["attempts"] != attempt:
                print(f"FAIL: after attempt {attempt}, entry shows "
                      f"attempts={entry['attempts']} (expected {attempt})")
                ok = False
        else:
            if not os.path.exists(fe.DEAD_LETTER_PATH):
                print(f"FAIL: entry was not dead-lettered after "
                      f"{fe.MAX_WRITE_ATTEMPTS} failed attempts")
                ok = False
            elif os.path.exists(fe.PENDING_WRITES_PATH):
                print("FAIL: entry still present in the pending file after "
                      "being dead-lettered (should be removed from it)")
                ok = False
            else:
                with open(fe.DEAD_LETTER_PATH) as f:
                    dl_entry = json.loads(f.readline())
                checks = [
                    (dl_entry["write_id"] == write_id, "write_id preserved"),
                    (dl_entry["attempts"] == fe.MAX_WRITE_ATTEMPTS, "attempts == MAX_WRITE_ATTEMPTS"),
                    ("last_error" in dl_entry and dl_entry["last_error"], "last_error recorded"),
                    ("dead_lettered_at" in dl_entry, "dead_lettered_at recorded"),
                    (dl_entry["payload"]["ticker"] == "TEST", "original payload preserved, not deleted"),
                ]
                if all(c[0] for c in checks):
                    print(f"PASS: entry dead-lettered on exactly attempt "
                          f"{fe.MAX_WRITE_ATTEMPTS}/{fe.MAX_WRITE_ATTEMPTS}, "
                          "with write_id/attempts/last_error/payload all "
                          "preserved in the dead-letter record.")
                else:
                    failed = [name for passed, name in checks if not passed]
                    print(f"FAIL: dead-letter record missing/wrong fields: {failed}")
                    ok = False

    # 2. A write that succeeds before reaching MAX_WRITE_ATTEMPTS is never
    #    dead-lettered and leaves no trace in either file.
    reset_state()
    fe.mm_journal = lambda op, payload: None
    try:
        fe.persist_or_raise("create_forecast", TEST_PAYLOAD)
    except fe.PersistenceError:
        pass
    fe.rewrite_pending_writes()  # attempts=1, still pending
    fe.mm_journal = lambda op, payload: {"forecast": {"id": "recovered"}}
    fe._run_write_outcomes.clear()
    fe._run_write_failures.clear()
    fe.flush_pending_writes()
    fe.rewrite_pending_writes()
    if os.path.exists(fe.PENDING_WRITES_PATH) or os.path.exists(fe.DEAD_LETTER_PATH):
        print("FAIL: a write recovered before MAX_WRITE_ATTEMPTS left a "
              "trace in the pending or dead-letter file")
        ok = False
    else:
        print("PASS: a write that recovers before exhausting its attempts "
              "leaves no trace in either file, same as before this fix.")

    # 3. Dead-lettering is append-only across multiple exhausted entries
    #    (a second, distinct write_id exhausting later doesn't clobber the
    #    first's record).
    reset_state()
    fe.mm_journal = lambda op, payload: None
    other_payload = dict(TEST_PAYLOAD, ticker="OTHER")
    for payload in (TEST_PAYLOAD, other_payload):
        for attempt in range(fe.MAX_WRITE_ATTEMPTS):
            fe._run_write_outcomes.clear()
            fe._run_write_failures.clear()
            if attempt == 0:
                try:
                    fe.persist_or_raise("create_forecast", payload)
                except fe.PersistenceError:
                    pass
            else:
                fe.flush_pending_writes()
            fe.rewrite_pending_writes()
        # each ticker's own entry independently reaches MAX_WRITE_ATTEMPTS
        # and gets dead-lettered before the next ticker is staged (staging
        # happens fresh each outer-loop iteration via persist_or_raise) --
        # but flush_pending_writes() at the top of each inner loop retries
        # whatever's currently pending, so the SECOND ticker's entry only
        # exists in the file once persist_or_raise() stages it below.
    with open(fe.DEAD_LETTER_PATH) as f:
        dl_lines = [json.loads(ln) for ln in f if ln.strip()]
    tickers = sorted(e["payload"]["ticker"] for e in dl_lines)
    if tickers == ["OTHER", "TEST"]:
        print("PASS: dead-lettering is append-only -- both independently "
              "exhausted entries survive in the dead-letter file, neither "
              "clobbers the other.")
    else:
        print(f"FAIL: expected dead-letter file to contain TEST and OTHER, "
              f"found {tickers}")
        ok = False

    reset_state()
    return 0 if ok else 1


def main():
    before_pending = _hash(REAL_PENDING_WRITES_PATH)
    before_dl = _hash(REAL_DEAD_LETTER_PATH)

    tmp_dir = tempfile.mkdtemp(prefix="dead_letter_test_")
    fe.PENDING_WRITES_PATH = os.path.join(tmp_dir, "pending_forecast_writes.jsonl")
    fe.DEAD_LETTER_PATH = os.path.join(tmp_dir, "dead_letter_forecast_writes.jsonl")
    try:
        rc = run_tests()
    finally:
        fe.PENDING_WRITES_PATH = REAL_PENDING_WRITES_PATH
        fe.DEAD_LETTER_PATH = REAL_DEAD_LETTER_PATH
        shutil.rmtree(tmp_dir, ignore_errors=True)

    after_pending = _hash(REAL_PENDING_WRITES_PATH)
    after_dl = _hash(REAL_DEAD_LETTER_PATH)
    if after_pending == before_pending and after_dl == before_dl:
        print("PASS: the real pending-writes and dead-letter queues were "
              "untouched by this test run.")
    else:
        print("FAIL: a real queue file changed during this test run -- it "
              "should have been fully isolated to a tempdir.")
        rc = 1

    return rc


if __name__ == "__main__":
    sys.exit(main())
