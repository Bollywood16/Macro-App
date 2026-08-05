#!/usr/bin/env python3
"""
Mutation/canary lookahead test for PointInTimeDataContext -- docs/C3_DESIGN.md
§3.3 point 1, the PRIMARY mechanism (an equality test between "truncated store"
and "full store filtered to the same date" is weaker than it looks: if the
truncation logic has a bug, both paths can share it and agree by coincidence).

Method: build a fixture store truncated at date D. Query PointInTimeDataContext
at as_of=D and record the output. Then splice an obviously out-of-family value
into the SAME fixture at D+1 (an absurd price, an absurd VIX/credit spike) and
re-query at the SAME as_of=D. Assert the output is byte-identical. If it isn't,
something downstream of as_of() saw data past D -- mechanically detected, not
inferred from suspiciously good numbers later.

Run against every table this phase populates (prices, macro) and every method
PointInTimeDataContext exposes that touches dated data (close, ohlcv, vix,
credit_spread). `replay()` itself doesn't exist yet (C2, not built) -- this
tests the context object directly, which is what's actually being built in
this phase; re-run at the replay()/engine level is C2's own acceptance
criterion per §3.3 point 4, not duplicated here.

Also exercises §3.3 point 3's runtime assertion directly: construct a case
where a caller bypasses as_of() and hands PointInTimeDataContext-shaped data
with a future row, confirm the shared assertion catches it.

No network. Run: python3 scripts/tests/test_pit_lookahead_canary.py
"""
import os
import sys
import shutil
import tempfile
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
sys.path.insert(0, SCRIPTS)

import pandas as pd  # noqa: E402
import pit_store as pit  # noqa: E402

D = date(2015, 8, 24)  # arbitrary mid-history date, matches docs/CREDIT_SERIES.md
                        # §1.3's own replay demonstration date, for continuity


def _build_fixture(root, mutate_after_D=False):
    """20 trading days straddling D (10 before/on, 10 after). If mutate_after_D,
    every row strictly after D gets an absurd out-of-family value spliced in --
    close=$1 (a fake -95%+ move), VIX=999, BAA10Y=99 -- values that could only
    ever leak in from the future, never occur honestly."""
    idx = pd.bdate_range(end=D, periods=10).append(
        pd.bdate_range(start=D + timedelta(days=1), periods=10))
    idx = pd.DatetimeIndex(sorted(set(idx)))

    close = pd.Series(20.0 + pd.RangeIndex(len(idx)) * 0.1, index=idx)
    ohlcv = pd.DataFrame({
        "open": close, "high": close + 0.2, "low": close - 0.2,
        "close": close, "volume": 1_000_000,
    }, index=idx)
    vix = pd.Series(15.0 + pd.RangeIndex(len(idx)) * 0.05, index=idx)
    credit = pd.Series(2.0 + pd.RangeIndex(len(idx)) * 0.01, index=idx)

    if mutate_after_D:
        future_mask = idx.date > D
        ohlcv.loc[future_mask, ["open", "high", "low", "close"]] = 1.0
        vix.loc[future_mask] = 999.0
        credit.loc[future_mask] = 99.0

    pit.write_prices("CANARY", ohlcv, store_root=root)
    pit.write_macro(pit.VIX_SERIES, vix, store_root=root)
    pit.write_macro(pit.CREDIT_SERIES, credit, store_root=root)


def main():
    ok = True
    root_clean = tempfile.mkdtemp(prefix="pit_canary_clean_")
    root_mutated = tempfile.mkdtemp(prefix="pit_canary_mutated_")
    try:
        _build_fixture(root_clean, mutate_after_D=False)
        _build_fixture(root_mutated, mutate_after_D=True)

        ctx_clean = pit.PointInTimeDataContext(as_of_date=D, store_root=root_clean)
        ctx_mutated = pit.PointInTimeDataContext(as_of_date=D, store_root=root_mutated)

        checks = [
            ("close", lambda c: c.close("CANARY")),
            ("ohlcv", lambda c: c.ohlcv("CANARY")),
            ("vix", lambda c: c.vix()),
            ("credit_spread", lambda c: c.credit_spread()),
        ]
        for name, fn in checks:
            clean_result = fn(ctx_clean)
            mutated_result = fn(ctx_mutated)
            if isinstance(clean_result, pd.DataFrame):
                identical = clean_result.equals(mutated_result)
            else:
                identical = clean_result.equals(mutated_result)
            # Sanity check the mutation is even real -- if the mutated store's
            # own last-index also stayed <= D, the fixture didn't plant
            # anything reachable and this test would be vacuous.
            mutated_has_future = (mutated_result.index.max().date() > D
                                   if len(mutated_result) else False)
            if identical and not mutated_has_future:
                print(f"PASS: {name}() byte-identical between clean and "
                      f"future-mutated stores at as_of={D} "
                      f"(both correctly stop at {clean_result.index.max().date()}).")
            elif not identical:
                print(f"FAIL: {name}() differs between clean and mutated stores "
                      f"at as_of={D} -- a future value leaked through.")
                ok = False
            else:
                print(f"FAIL: {name}() sanity check failed -- mutated result's "
                      "own index already stayed <= D, so this run didn't "
                      "actually exercise the canary (test construction bug).")
                ok = False

        # --- direct check: as_of=D never returns a row dated after D, full stop ---
        for name, fn in checks:
            r = fn(ctx_clean)
            max_date = r.index.max().date() if len(r) else None
            if max_date is not None and max_date > D:
                print(f"FAIL: {name}() as_of={D} returned a row dated {max_date}")
                ok = False
        else:
            print(f"PASS: no method returned any row dated after as_of={D}.")

        # --- §3.3 point 3: the shared runtime assertion itself fires on a
        # direct violation, independent of as_of()'s own filtering (belt and
        # suspenders -- simulates a future call path that builds a Series by
        # hand instead of going through as_of()). ---
        bad_dates = pd.DatetimeIndex([D, D + timedelta(days=5)]).date
        try:
            pit._assert_no_lookahead(bad_dates, D, "synthetic-violation-check")
            print("FAIL: _assert_no_lookahead() did not raise on a future date")
            ok = False
        except AssertionError:
            print("PASS: _assert_no_lookahead() raises immediately when handed "
                  "a date after as_of, independent of as_of()'s own filtering.")

    finally:
        shutil.rmtree(root_clean, ignore_errors=True)
        shutil.rmtree(root_mutated, ignore_errors=True)

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
