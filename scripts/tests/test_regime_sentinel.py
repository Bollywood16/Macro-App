#!/usr/bin/env python3
"""
Regression test for the "unknown" must never match "unknown" fix in
regime_conditioned_positions() (forecast_engine.py) and _matched_positions()
(dip_context.py). See docs/CREDIT_SERIES.md for how this was found: with
HY OAS only covering 2023-08-07+, every credit label before that date is
"unknown", and Python string equality happily matched "unknown" =="unknown"
-- so any pre-2023-08-07 query date got a fake full-depth "regime match"
against 20+ years of dates it shares nothing with except missing data.

Unit-level, synthetic regime tuples, no network, no real price history --
isolates the matching logic itself from everything else in the pipeline.

Run: python3 scripts/tests/test_regime_sentinel.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
sys.path.insert(0, SCRIPTS)
sys.path.insert(0, os.path.join(os.path.dirname(SCRIPTS), "engines"))

import forecast_engine as fe  # noqa: E402
import dip_context as dc  # noqa: E402


def main():
    ok = True

    # ---- forecast_engine.regime_conditioned_positions ----

    # Case 1: current tuple's credit dim is "unknown" (a pre-2023-08-07-
    # style replay query). 30 historical dates ALSO have unknown credit,
    # sharing nothing else in common with "today" at depth 3. Pre-fix,
    # these would have matched at depth 3 (30 >= MIN_REGIME_N). Post-fix,
    # depth 3 and depth 2 (both include the credit slot) must be forced to
    # zero candidates, backing off to depth 1 (vix-only, no unknowns) or
    # deeper.
    current = ("stressed", "unknown", "below")
    regime_tuples = [("stressed", "unknown", "below")] * 30  # would all "match" at depth 3 pre-fix
    regime_tuples += [("calm", "flat", "above")] * 5          # genuinely different regime
    pos, depth = fe.regime_conditioned_positions(regime_tuples, current, gap=1, min_n=8)
    if depth >= 2:
        print(f"FAIL: forecast_engine depth={depth} (>=2) -- credit-unknown "
              f"query still matched on the credit dimension")
        ok = False
    else:
        print(f"PASS: forecast_engine -- current tuple's own credit='unknown' "
              f"forces depth<=1 (got depth={depth}, n={len(pos)}), can't fake-match "
              f"on a dimension we don't have.")

    # Case 2: current tuple fully known, some historical rows have unknown
    # credit. Those rows must never be selected at depth>=2, same as
    # before the fix (equality already excluded them) -- confirms the fix
    # didn't regress the already-correct live-query case.
    current2 = ("elevated", "flat", "above")
    known_matches = [("elevated", "flat", "above")] * 10
    unknown_noise = [("elevated", "unknown", "above")] * 10  # same vix/spy, unknown credit
    regime_tuples2 = known_matches + unknown_noise
    pos2, depth2 = fe.regime_conditioned_positions(regime_tuples2, current2, gap=1, min_n=8)
    if depth2 != 3:
        print(f"FAIL: forecast_engine live-style case -- expected depth=3, got {depth2}")
        ok = False
    elif len(pos2) != 10 or any(p >= 10 for p in pos2):
        print(f"FAIL: forecast_engine live-style case -- expected exactly the 10 "
              f"known-match rows, got positions={pos2}")
        ok = False
    else:
        print("PASS: forecast_engine -- known current tuple still correctly matches "
              "only fully-known historical rows at depth=3, unknown-credit noise excluded.")

    # ---- dip_context._matched_positions (mirrors the same guard) ----
    dip_positions = list(range(35))
    dc_tuples = [("stressed", "unknown", "below")] * 30 + [("calm", "flat", "above")] * 5
    dc_pos, dc_depth = dc._matched_positions(dc_tuples, dip_positions, current, gap=1, min_n=8)
    if dc_depth >= 2:
        print(f"FAIL: dip_context depth={dc_depth} (>=2) -- same sentinel bug present")
        ok = False
    else:
        print(f"PASS: dip_context -- mirrors the same guard (depth={dc_depth}, n={len(dc_pos)}).")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
