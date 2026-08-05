#!/usr/bin/env python3
"""
Regression test for B4 (fail-loud persistence + write-ahead staging).
Formalizes what was verified interactively when this shipped: a failed
create_forecast/create_quote_snapshot write is staged BEFORE the attempt
(so a hard crash mid-request still leaves a retriable record), raises
PersistenceError instead of silently returning None, and is recovered by
the next run's flush_pending_writes() -- never lost to a transient error.

Unit-level (mocks mm_journal directly rather than running a full
run_one()) so this stays fast enough to run every time, not just when
someone remembers to check manually.

IMPORTANT: this test used to read/write/remove `forecast_engine.
PENDING_WRITES_PATH` directly -- the REAL `data/pending_forecast_writes.jsonl`
queue, Fix 5's write-ahead recovery log for forecasts that failed to persist.
Running this test during a real outage could have destroyed genuinely queued,
not-yet-recovered forecasts. Fixed here: `fe.PENDING_WRITES_PATH` is
monkeypatched to a tempdir path for the duration of every test in this file,
restored in a `finally`, and the real file's contents are hashed before/after
the whole run to assert it was never touched -- not just avoided by
convention, checked.

Run: python3 scripts/tests/test_fail_loud_persistence.py
"""
import hashlib
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
    "ticker": "TEST", "trading_date": "2026-08-05", "as_of_ts": "2026-08-05T12:00:00+00:00",
    "horizon_days": 5, "model_version": "test-model", "voter": "forecast",
}

REAL_PENDING_WRITES_PATH = fe.PENDING_WRITES_PATH  # captured before any patching


def _hash_real_queue():
    """None if the real queue file doesn't exist, else a content hash --
    covers both "must still not exist" and "must be byte-for-byte unchanged"
    without having to special-case either in the caller."""
    if not os.path.exists(REAL_PENDING_WRITES_PATH):
        return None
    with open(REAL_PENDING_WRITES_PATH, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def reset_staging():
    fe._run_write_outcomes.clear()
    if os.path.exists(fe.PENDING_WRITES_PATH):
        os.remove(fe.PENDING_WRITES_PATH)


def run_tests():
    ok = True
    reset_staging()

    # 1. A failing write stages to disk BEFORE the attempt and raises.
    fe.mm_journal = lambda op, payload: None  # simulates every write failing
    try:
        fe.persist_or_raise("create_forecast", TEST_PAYLOAD)
        print("FAIL: persist_or_raise did not raise on a failed write")
        ok = False
    except fe.PersistenceError:
        if os.path.exists(fe.PENDING_WRITES_PATH):
            with open(fe.PENDING_WRITES_PATH) as f:
                staged = [ln for ln in f if ln.strip()]
            if len(staged) == 1:
                print("PASS: failed write raised PersistenceError and staged "
                      "exactly 1 entry to disk.")
            else:
                print(f"FAIL: expected 1 staged entry, found {len(staged)}")
                ok = False
        else:
            print("FAIL: PersistenceError raised but nothing was staged to disk")
            ok = False

    # 2. The next run's flush recovers it once the write actually succeeds,
    #    and rewrite_pending_writes() clears the file.
    fe.mm_journal = lambda op, payload: {"forecast": {"id": "recovered"}}
    fe.flush_pending_writes()
    fe.rewrite_pending_writes()
    if not os.path.exists(fe.PENDING_WRITES_PATH):
        print("PASS: a subsequent successful run recovered the staged write "
              "and cleared the queue file.")
    else:
        print("FAIL: staged write was not cleared after a successful retry")
        ok = False

    # 3. A write that succeeds on its first attempt never gets left behind.
    reset_staging()
    fe.mm_journal = lambda op, payload: {"forecast": {"id": "ok"}}
    fe.persist_or_raise("create_forecast", TEST_PAYLOAD)
    fe.rewrite_pending_writes()
    if not os.path.exists(fe.PENDING_WRITES_PATH):
        print("PASS: a successful write leaves no staged entry behind.")
    else:
        print("FAIL: a successful write was left in the queue file")
        ok = False

    reset_staging()
    return 0 if ok else 1


def main():
    before_hash = _hash_real_queue()

    tmp_dir = tempfile.mkdtemp(prefix="pending_writes_test_")
    tmp_path = os.path.join(tmp_dir, "pending_forecast_writes.jsonl")
    fe.PENDING_WRITES_PATH = tmp_path  # every fe.* function reads this global
                                        # at call time, so this redirects
                                        # stage/flush/rewrite for the whole run
    try:
        rc = run_tests()
    finally:
        fe.PENDING_WRITES_PATH = REAL_PENDING_WRITES_PATH
        shutil.rmtree(tmp_dir, ignore_errors=True)

    after_hash = _hash_real_queue()
    if after_hash == before_hash:
        print("PASS: the real data/pending_forecast_writes.jsonl queue was "
              "untouched by this test run.")
    else:
        print("FAIL: the real pending-writes queue changed during this test "
              "run -- it should have been fully isolated to a tempdir.")
        rc = 1

    return rc


if __name__ == "__main__":
    sys.exit(main())
