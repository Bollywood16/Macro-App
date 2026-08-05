#!/usr/bin/env python3
"""
pit_seed.py — one-shot local seed for the C3 Parquet store, NOT C4's backfill.

This is explicitly not `MARKET_MEMORY_V2_BUILD.md` §4's C4 workflow ("dedicated
GitHub Actions workflow... chunked and resumable... report block count per
horizon on completion"). It exists so C3's own code (`as_of()`,
`PointInTimeDataContext`, the mutation/canary tests) has real data to run
against instead of only synthetic fixtures -- 18 sequential yfinance/FRED calls,
run once, locally, no chunking or resumability needed at this size. C4's actual
job (17 tickers x full 2005-2025 daily bars, `forecasts_replay` writes, per-
horizon block-count reporting) is unrelated and unbuilt.

Populates:
  - macro/BAA10Y  -- full available history (FRED serves back to 1986-01-02),
    which clears docs/C3_DESIGN.md §2.4's "no later than 1999-01-01" requirement
    for credit_regime_series()'s 1260-day warmup with several years to spare.
  - macro/VIX     -- full available history.
  - prices/{ticker} for the 17 replay tickers (docs/C3_DESIGN.md §2.3's list:
    the 5 in universe_config.json + the 14 in rotation_config.json minus
    XLC/XLRE).

Run: python3 scripts/pit_seed.py [--store-root PATH]
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import research_engine as re_engine  # noqa: E402
import pit_store  # noqa: E402

REPLAY_TICKERS = [
    "SMH", "^SOX", "SPY", "QQQ", "GLD",           # universe_config.json
    "XLK", "XLF", "XLV", "XLE", "XLI", "XLY", "XLP", "XLU", "XLB",  # sectors minus XLC/XLRE
    "RSP", "IWM", "MGK",                           # size_style
]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--store-root", default=pit_store.DEFAULT_STORE_ROOT)
    args = ap.parse_args()

    print(f"Seeding PIT store at {args.store_root}")

    print("\n--- macro ---")
    baa10y = re_engine.fetch_credit_spread()
    if baa10y.empty:
        print("[error] BAA10Y fetch returned empty -- aborting macro seed")
    else:
        earliest = baa10y.index.min().date()
        n = pit_store.write_macro(pit_store.CREDIT_SERIES, baa10y, args.store_root)
        floor_ok = earliest <= pit_store.CREDIT_WARMUP_FLOOR
        print(f"  BAA10Y: {n} rows, {earliest} -> {baa10y.index.max().date()}  "
              f"(clears {pit_store.CREDIT_WARMUP_FLOOR} warmup floor: {floor_ok})")
        if not floor_ok:
            print(f"  [error] BAA10Y earliest date {earliest} is AFTER the "
                  f"{pit_store.CREDIT_WARMUP_FLOOR} warmup floor required by "
                  "docs/C3_DESIGN.md #2.4 -- the 1260-day percentile window "
                  "will render 'unknown' for the early replay years.")

    vix = re_engine.fetch_history("^VIX")
    if vix.empty:
        print("[error] VIX fetch returned empty -- aborting macro seed")
    else:
        n = pit_store.write_macro(pit_store.VIX_SERIES, vix, args.store_root)
        print(f"  VIX: {n} rows, {vix.index.min().date()} -> {vix.index.max().date()}")

    print("\n--- prices ---")
    results = []
    for ticker in REPLAY_TICKERS:
        try:
            ohlcv = re_engine.fetch_ohlcv(ticker)
            n = pit_store.write_prices(ticker, ohlcv, args.store_root)
            first, last = ohlcv.index.min().date(), ohlcv.index.max().date()
            covers_2005 = first <= __import__("datetime").date(2005, 1, 1)
            print(f"  {ticker:<6} {n:>6} rows  {first} -> {last}  "
                  f"(covers 2005-01-01: {covers_2005})")
            results.append((ticker, n, first, last, covers_2005))
        except Exception as e:
            print(f"  [error] {ticker}: {e}")
            results.append((ticker, 0, None, None, False))

    n_ok = sum(1 for r in results if r[1] > 0)
    print(f"\n{n_ok}/{len(REPLAY_TICKERS)} tickers seeded.")
    missing_2005 = [r[0] for r in results if r[1] > 0 and not r[4]]
    if missing_2005:
        print(f"Tickers whose history does NOT cover 2005-01-01 (expected -- "
              f"docs/C3_DESIGN.md #2.3 flags MGK specifically): {missing_2005}")


if __name__ == "__main__":
    main()
