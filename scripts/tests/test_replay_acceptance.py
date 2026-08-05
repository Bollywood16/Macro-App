#!/usr/bin/env python3
"""
C2 acceptance test, `MARKET_MEMORY_V2_BUILD.md` §4's exact wording:

    Acceptance: replay('SPY', <random 2019 date>) byte-identical when run
    against a store truncated at that date.

This is a STRONGER test than `test_pit_lookahead_canary.py`'s mutation check
(which splices an implausible value after D into the same store and checks the
output doesn't change). Here, two PHYSICALLY SEPARATE stores are built: the
full real `data/pit` store (seeded through 2026-08-05, i.e. genuinely has ~7
more years of real data past the query date) and a copy of it with every row
after D actually deleted, not just present-and-ignored. `replay('SPY', D)`
against both must produce byte-identical output -- proves the ONLY thing
controlling what's visible is `as_of()`'s `processed_date <= D` filter, not
"how much data happens to exist beyond D," which the mutation test alone
can't fully rule out (a bug that only manifests when future rows are entirely
absent vs. present-but-wrong wouldn't be caught by mutation alone).

Requires the real `data/pit` store to already be seeded (`python3 scripts/
pit_seed.py`) -- `data/pit/` is gitignored (derived data, not committed), so
this SKIPS (not fails) with a clear message if it isn't present, rather than
reporting a false failure on a fresh checkout.

The random 2019 date is deterministic (seeded), not re-rolled per run, so a
failure is reproducible -- but the seed is printed so it's clear this wasn't
cherry-picked.

Run: python3 scripts/tests/test_replay_acceptance.py
"""
import json
import os
import random
import shutil
import sys
import tempfile
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
sys.path.insert(0, SCRIPTS)

import pandas as pd  # noqa: E402
import pit_store as pit  # noqa: E402
from replay import replay  # noqa: E402

REQUIRED_KEYS = [
    (pit.PRICES_TABLE, "SPY"),
    # relative_strength's on-demand Nasdaq-100 benchmark (resolve_benchmarks())
    # -- forecast_engine.py fetches this via ctx.close("QQQ") even when the
    # ticker being replayed is SPY itself. Omitting it from a truncated copy
    # doesn't corrupt the comparison (ctx.close() just raises and
    # relative_strength's `except Exception` warns+drops that benchmark
    # column identically on both sides) -- but it WOULD make the two runs
    # differ if only one side has it, which is exactly what the first version
    # of this test caught before QQQ was added here. Kept as a live
    # illustration of why truncated-copy coverage has to match every ticker a
    # tearsheet engine can reach for, not just the one being replayed.
    (pit.PRICES_TABLE, "QQQ"),
    (pit.MACRO_TABLE, pit.VIX_SERIES),
    (pit.MACRO_TABLE, pit.CREDIT_SERIES),
]


def _store_seeded(root: str) -> bool:
    return all(os.path.isdir(pit._key_dir(table, key, root)) for table, key in REQUIRED_KEYS)


def _build_truncated_copy(src_root: str, dst_root: str, cutoff: date):
    """Physically copies every parquet file under src_root, dropping any row
    whose effective_date > cutoff -- not just filtering at read time. A
    year-file that ends up with zero rows after the cutoff is skipped
    entirely, matching what a real from-scratch truncated ingest would look
    like (no empty files)."""
    for table, key in REQUIRED_KEYS:
        src_dir = pit._key_dir(table, key, src_root)
        dst_dir = pit._key_dir(table, key, dst_root)
        os.makedirs(dst_dir, exist_ok=True)
        for fname in sorted(os.listdir(src_dir)):
            if not fname.endswith(".parquet"):
                continue
            df = pd.read_parquet(os.path.join(src_dir, fname))
            df["effective_date"] = pd.to_datetime(df["effective_date"]).dt.date
            kept = df[df["effective_date"] <= cutoff]
            if len(kept):
                kept.to_parquet(os.path.join(dst_dir, fname), index=False)


def _random_2019_date(seed: int = 20260805) -> date:
    rng = random.Random(seed)
    start = date(2019, 1, 2).toordinal()
    end = date(2019, 12, 31).toordinal()
    return date.fromordinal(rng.randint(start, end))


def _deep_equal_report(a, b, path=""):
    """Returns a list of human-readable diffs (empty if byte-identical).
    Recurses through dicts/lists; compares everything else with ==. Used
    instead of a bare assert so a failure (which should not happen, but if
    it ever does) points at exactly which field diverged."""
    diffs = []
    if isinstance(a, dict) and isinstance(b, dict):
        keys = set(a) | set(b)
        for k in sorted(keys, key=str):
            diffs += _deep_equal_report(a.get(k, "<MISSING>"), b.get(k, "<MISSING>"), f"{path}.{k}")
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            diffs.append(f"{path}: length {len(a)} vs {len(b)}")
        else:
            for i, (x, y) in enumerate(zip(a, b)):
                diffs += _deep_equal_report(x, y, f"{path}[{i}]")
    else:
        if a != b:
            diffs.append(f"{path}: {a!r} vs {b!r}")
    return diffs


def main():
    if not _store_seeded(pit.DEFAULT_STORE_ROOT):
        print("SKIP: data/pit is not seeded (run `python3 scripts/pit_seed.py` "
              "first). data/pit/ is gitignored, so this is expected on a "
              "fresh checkout -- not a failure.")
        return 0

    query_date = _random_2019_date()
    print(f"C2 acceptance test: replay('SPY', {query_date}) "
          f"(seed=20260805, full 2019 range)")

    tmp_root = tempfile.mkdtemp(prefix="pit_truncated_")
    try:
        _build_truncated_copy(pit.DEFAULT_STORE_ROOT, tmp_root, query_date)

        result_full = replay("SPY", query_date, store_root=pit.DEFAULT_STORE_ROOT)
        result_truncated = replay("SPY", query_date, store_root=tmp_root)

        if result_full is None or result_truncated is None:
            print(f"FAIL: replay() returned None (full={result_full is None}, "
                  f"truncated={result_truncated is None}) -- insufficient "
                  "history as-of this date, cannot run the comparison.")
            return 1

        # Sanity: prove the truncated store really is smaller (i.e. this
        # test isn't vacuously comparing a store against an identical copy
        # of itself) -- the full store has ~7 more years of SPY history
        # past query_date that the truncated copy must not have.
        full_ohlcv_rows = sum(
            len(pd.read_parquet(os.path.join(pit._key_dir("prices", "SPY", pit.DEFAULT_STORE_ROOT), f)))
            for f in os.listdir(pit._key_dir("prices", "SPY", pit.DEFAULT_STORE_ROOT))
            if f.endswith(".parquet"))
        truncated_ohlcv_rows = sum(
            len(pd.read_parquet(os.path.join(pit._key_dir("prices", "SPY", tmp_root), f)))
            for f in os.listdir(pit._key_dir("prices", "SPY", tmp_root))
            if f.endswith(".parquet"))
        if truncated_ohlcv_rows >= full_ohlcv_rows:
            print(f"FAIL: sanity check failed -- truncated store has "
                  f"{truncated_ohlcv_rows} SPY price rows, full store has "
                  f"{full_ohlcv_rows}. Truncation didn't remove anything; "
                  "this run would not have exercised the acceptance test.")
            return 1
        print(f"  (sanity: full store {full_ohlcv_rows} SPY price rows, "
              f"truncated copy {truncated_ohlcv_rows} rows -- "
              f"{full_ohlcv_rows - truncated_ohlcv_rows} rows physically absent "
              "from the truncated copy, all dated after the query date)")

        diffs = _deep_equal_report(result_full, result_truncated)
        if not diffs:
            print(f"PASS: replay('SPY', {query_date}) is byte-identical between "
                  "the full store (with ~7 years of data past the query date "
                  "still present) and a physically truncated copy of the same "
                  "store. n_independent (basis horizon)="
                  f"{result_full['primary_horizon']['ensemble']['n']}, "
                  f"recommendation={result_full['recommendation_label']!r}, "
                  f"confidence={result_full['confidence_label']!r}.")
            return 0
        else:
            print(f"FAIL: {len(diffs)} field(s) differ between full-store and "
                  "truncated-store replay output:")
            for d in diffs[:20]:
                print(f"    {d}")
            return 1
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
