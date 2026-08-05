#!/usr/bin/env python3
"""
Direct unit test of pit_store.as_of(), independent of any engine -- docs/
C3_DESIGN.md §3.3 point 2. Builds tiny synthetic fixtures with rows straddling
a boundary date and checks the filter itself, not anything downstream.

Covers:
  1. Basic boundary: processed_date <= date is included, processed_date > date
     is excluded, for a table where effective_date == processed_date (prices'
     own shape).
  2. The flows-table failure mode §2.2 names by example: a row whose
     effective_date is ON OR BEFORE the query date but whose processed_date is
     AFTER it must still be excluded -- an NAV figure *for* day D that wasn't
     *published* until D+2 must not leak into a D+1 (or even a D+1-week) query,
     even though naively filtering on effective_date alone would let it through.
     No real flows fetcher exists yet (§2.5), so this is tested against
     pit_store's write_macro() path with a hand-built lagged fixture, not a
     "flows" writer.
  3. Empty-key-directory and empty-after-filter both raise PITStoreError, with
     distinguishable messages (§2.3's "no data ingested" vs. "data exists, none
     as-of this date").

No network. Run: python3 scripts/tests/test_pit_as_of.py
"""
import os
import sys
import shutil
import tempfile
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
sys.path.insert(0, SCRIPTS)

import pandas as pd  # noqa: E402
import pit_store as pit  # noqa: E402


def main():
    ok = True
    tmp = tempfile.mkdtemp(prefix="pit_as_of_test_")
    try:
        # --- 1: basic boundary, prices-shaped (effective_date == processed_date) ---
        idx = pd.date_range("2020-01-01", periods=5, freq="D")
        ohlcv = pd.DataFrame({
            "open": range(5), "high": range(5), "low": range(5),
            "close": [10.0, 11.0, 12.0, 13.0, 14.0], "volume": [100] * 5,
        }, index=idx)
        pit.write_prices("TEST", ohlcv, store_root=tmp)

        rows = pit.as_of("prices", "TEST", date(2020, 1, 3), store_root=tmp)
        got_dates = sorted(rows["effective_date"].tolist())
        expected = [date(2020, 1, 1), date(2020, 1, 2), date(2020, 1, 3)]
        if got_dates == expected:
            print("PASS: as_of() boundary is inclusive of the query date, "
                  "excludes everything after it (prices-shaped).")
        else:
            print(f"FAIL: expected {expected}, got {got_dates}")
            ok = False

        # --- 2: effective_date <= date but processed_date > date must be excluded ---
        # Hand-built lagged fixture: a value "for" 2020-01-04 that wasn't
        # "published" until 2020-01-06 (T+2), same shape flows/NAV data would
        # have once Phase F wires up a real fetcher. write_macro() always sets
        # processed_date == effective_date, so this fixture is built directly
        # against the same parquet layout instead, to isolate as_of()'s filter
        # from any writer's own same-day assumption.
        lagged_dir = os.path.join(tmp, "flows", "TEST")
        os.makedirs(lagged_dir, exist_ok=True)
        lagged = pd.DataFrame({
            "effective_date": [date(2020, 1, 3), date(2020, 1, 4), date(2020, 1, 5)],
            "processed_date": [date(2020, 1, 3), date(2020, 1, 6), date(2020, 1, 7)],
            "value": [1.0, 2.0, 3.0],
        })
        lagged.to_parquet(os.path.join(lagged_dir, "2020.parquet"), index=False)

        # Query at 2020-01-04: effective_date-only filtering would wrongly
        # include the 2020-01-04 row (published 01-06, not yet known on 01-04).
        rows_lag = pit.as_of("flows", "TEST", date(2020, 1, 4), store_root=tmp)
        got_eff = sorted(rows_lag["effective_date"].tolist())
        if got_eff == [date(2020, 1, 3)]:
            print("PASS: a row published (processed_date) after the query date "
                  "is excluded even though its effective_date is on/before it "
                  "(the flows-table lag case).")
        else:
            print(f"FAIL: expected only the 01-03 row, got {got_eff}")
            ok = False

        # Query at 2020-01-06: the 01-04 row (published 01-06) should now show
        # up, the 01-05 row (published 01-07) should not.
        rows_lag2 = pit.as_of("flows", "TEST", date(2020, 1, 6), store_root=tmp)
        got_eff2 = sorted(rows_lag2["effective_date"].tolist())
        if got_eff2 == [date(2020, 1, 3), date(2020, 1, 4)]:
            print("PASS: as publication date advances, the lagged row becomes "
                  "visible exactly on its own processed_date, not before.")
        else:
            print(f"FAIL: expected [01-03, 01-04], got {got_eff2}")
            ok = False

        # --- 3: empty cases raise distinguishable PITStoreError messages ---
        try:
            pit.as_of("prices", "NEVER_INGESTED", date(2020, 1, 1), store_root=tmp)
            print("FAIL: as_of() on a never-ingested key did not raise")
            ok = False
        except pit.PITStoreError as e:
            if "no data ever ingested" in str(e):
                print("PASS: as_of() on a never-ingested key raises with a "
                      "distinguishable message.")
            else:
                print(f"FAIL: raised, but message doesn't say 'never ingested': {e}")
                ok = False

        try:
            pit.as_of("prices", "TEST", date(2019, 1, 1), store_root=tmp)
            print("FAIL: as_of() before any available data did not raise")
            ok = False
        except pit.PITStoreError as e:
            if "first available effective_date" in str(e):
                print("PASS: as_of() before a key's earliest data raises with "
                      "the first-available date named (§2.3).")
            else:
                print(f"FAIL: raised, but message doesn't name first-available date: {e}")
                ok = False

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
