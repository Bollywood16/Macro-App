#!/usr/bin/env python3
"""
Regression test for analog_positions()'s lookahead guard (see docs/
CREDIT_SERIES.md and docs/C3_DESIGN.md §3.2 for how this was found: the
function only ever excluded query_pos itself, not rows after it -- safe on
the live path only by accident of query_pos always being the last row).

Two things to prove:
  1. No live-behavior change: query_pos == len(X)-1 (today's actual call
     pattern) returns identical candidates whether or not the guard is
     applied, because there's nothing after the last row to exclude either
     way.
  2. The guard actually guards: with query_pos pointing partway through a
     longer array (what a naive replay() caller might pass instead of
     truncating first), rows after query_pos are never returned as
     candidates -- and are provably influencing the old code's distance
     ranking before the fix, via a planted near-duplicate placed only in
     the "future" half of the array.

Unit-level, no network, no forecast_engine.run_one() -- exercises
analog_positions() directly.

Run: python3 scripts/tests/test_analog_positions_no_lookahead.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
sys.path.insert(0, SCRIPTS)
sys.path.insert(0, os.path.join(os.path.dirname(SCRIPTS), "engines"))

import numpy as np  # noqa: E402
import forecast_engine as fe  # noqa: E402


def make_matrix(n_rows, n_features=3, seed=0):
    rng = np.random.default_rng(seed)
    return rng.normal(size=(n_rows, n_features))


def main():
    ok = True

    # --- 1: live call pattern (query_pos == last row) is unaffected ---
    X = make_matrix(80)
    query_pos = len(X) - 1
    kept, dist = fe.analog_positions(X, query_pos)
    if any(p >= query_pos for p in kept):
        print("FAIL: live-pattern call returned a candidate at/after query_pos "
              f"(query_pos={query_pos}, kept={kept})")
        ok = False
    else:
        print("PASS: live call pattern (query_pos = last row) -- "
              f"{len(kept)} candidates, all strictly before query_pos, as before.")

    # --- 2: replay-style call (query_pos partway through a longer array) ---
    X2 = make_matrix(80)
    replay_query_pos = 40
    # Plant a near-exact duplicate of the query row just AFTER query_pos --
    # the single closest possible "analog" by construction. Pre-fix, this
    # is exactly the row analog_positions() would rank #1 (lookahead).
    X2[55] = X2[replay_query_pos] + 1e-6

    kept2, dist2 = fe.analog_positions(X2, replay_query_pos)
    future_leak = [p for p in kept2 if p >= replay_query_pos]
    if future_leak:
        print(f"FAIL: replay-style call leaked future row(s) as candidates: {future_leak}")
        ok = False
    elif 55 in kept2:
        print("FAIL: planted future near-duplicate (row 55) was returned as a candidate")
        ok = False
    else:
        print("PASS: replay-style call (query_pos=40 inside an 80-row array) -- "
              f"{len(kept2)} candidates, none at/after query_pos; planted future "
              "near-duplicate at row 55 correctly excluded.")

    # --- 3: sanity -- the planted duplicate WOULD have been picked without the guard ---
    # Reproduce the pre-fix selection logic inline (not calling the fixed
    # function) to prove this isn't a vacuous test -- the near-duplicate
    # really is the nearest neighbor once future rows are eligible.
    valid_old = ~np.isnan(X2).any(axis=1)
    valid_old[replay_query_pos] = False  # pre-fix guard: only excludes the query row itself
    diffs = X2[valid_old] - X2[replay_query_pos]
    dists = np.sqrt(np.sum(diffs ** 2, axis=1))
    valid_positions = np.where(valid_old)[0]
    nearest_old = valid_positions[np.argmin(dists)]
    if nearest_old == 55:
        print("PASS: confirmed the pre-fix guard would have ranked the planted "
              "future row as the single nearest analog -- the test is not vacuous.")
    else:
        print(f"FAIL: sanity check itself failed -- pre-fix nearest was {nearest_old}, "
              "expected 55. Test construction is broken, not the fix.")
        ok = False

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
