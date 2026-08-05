#!/usr/bin/env python3
"""
replay.py — Phase C2: `replay(ticker, date)`.

`MARKET_MEMORY_V2_BUILD.md` §4's exact contract:

    def replay(ticker: str, date: date) -> TearsheetBundle:
        '''Reconstruct the bundle the app would have produced at that close,
        using only as_of() data. Deterministic: same inputs -> byte-identical
        output.'''

    The live path becomes replay(ticker, today).

    Acceptance: replay('SPY', <random 2019 date>) byte-identical when run
    against a store truncated at that date.

No `TearsheetBundle` type exists anywhere in this codebase (the spec's pseudocode
names it, nothing defines it) -- the closest concrete equivalent is the dict
`forecast_engine.run_one()` already returns, extended (this phase) to also carry
`tearsheet_extras` at the top level, since that's the bulk of what a tear sheet
actually shows (dip_context/tech_read/bottom_scenarios/relative_strength/
episodes/triggers/agreement) and previously only reached a persisted
`evidence_json`, never the caller directly. `replay()` returns exactly that dict
-- not a new type, reusing the one that already exists.

How this satisfies docs/C3_DESIGN.md §3.2's conclusion ("replay() must truncate
every series/frame at the DataContext boundary -- never pass a full-length array
with an index pointer into forecast_engine.py's analog functions"): every series
handed to `run_one()` below (`close`, `spy_close`, `vix`, `oas`) comes from
`PointInTimeDataContext(as_of=date)`, i.e. already filtered by `as_of()` to
`processed_date <= date` before this function ever sees it. There is no "full
series + a pointer" anywhere in this call path -- `query_pos = len(df) - 1`
inside `run_one()` is trivially the truncated series' own last row, and the
`assert query_pos == len(df) - 1` added there this phase is redundant-by-
construction for this specific call path (insurance for a *different*, less
careful caller, not for this one).

`ctx.ohlcv(ticker)` inside `compute_tearsheet_extras()` (dip_context's volume
forensics, tech_read, bottom_scenarios) goes through the same
`PointInTimeDataContext` instance passed in here -- C1's whole point was making
`LiveDataContext`/`PointInTimeDataContext` interchangeable at every call site
that matters, and this is the first real exercise of that promise.

`episodes()` returns `None` on `PointInTimeDataContext` (no PIT episode store
exists -- see `pit_store.py`'s own docstring) -- `relative_strength`/
`bottom_scenarios`' episode-dependent reads degrade the same way they already do
today whenever `mm_journal`'s live episode read comes back empty, not a new
failure mode this phase introduces.

Persistence: always `dry_run=True`. Writing replay output to
`forecasts_replay` is C4's job (a separate table, chunked/resumable workflow,
block-count reporting) -- this function only computes and returns.
"""
from __future__ import annotations

import os
import sys
from datetime import date as date_type

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "engines"))

import forecast_engine as fe  # noqa: E402
import pit_store  # noqa: E402


def replay(ticker: str, date, store_root: str = pit_store.DEFAULT_STORE_ROOT) -> dict | None:
    """Reconstruct the bundle `forecast_engine.run_one()` would have produced
    for `ticker` at the close on or before `date`, using only
    `PointInTimeDataContext(as_of=date)` data. Returns `None` if `run_one()`
    itself declines to score (e.g. fewer than 260 trading days of history
    available as-of `date` -- the same "insufficient history" guard the live
    path already has, not a new replay-specific rule)."""
    ticker = ticker.upper()
    ctx = pit_store.PointInTimeDataContext(as_of_date=date, store_root=store_root)

    close = ctx.close(ticker)
    spy_close = close if ticker == fe.BENCHMARK else ctx.close(fe.BENCHMARK)
    spy_trend_df = fe.spy_trend_frame(spy_close)
    vix = ctx.vix()
    oas = ctx.credit_spread()

    universe_prices = {ticker: close}
    if fe.BENCHMARK not in universe_prices:
        universe_prices[fe.BENCHMARK] = spy_close

    asset = {"ticker": ticker, "label": ticker}
    # rotation_ctx/analog_map are live-only enrichments (sector-rotation
    # leadership context, sector-analog peer map) with no PIT-store backing
    # yet -- omitted here (None), same fail-soft posture build_feature_frame()
    # already has for a live run where rotation_ctx fetch fails (imputed to a
    # neutral 0.0/mid-pack default, not a crash). Not a correctness gap for
    # this phase's acceptance test (SPY's own regime/analog path doesn't
    # depend on either), flagged for whoever wires rotation into C3/C4.
    return fe.run_one(
        asset, universe_prices, spy_close, spy_trend_df, vix, oas,
        manual_price=None, market_status="closed", dry_run=True, ctx=ctx,
        rotation_ctx=None, source="replay", analog_map=None,
    )


if __name__ == "__main__":
    import argparse
    import json

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("ticker")
    ap.add_argument("date", help="YYYY-MM-DD")
    ap.add_argument("--store-root", default=pit_store.DEFAULT_STORE_ROOT)
    args = ap.parse_args()

    d = date_type.fromisoformat(args.date)
    result = replay(args.ticker, d, store_root=args.store_root)
    if result is None:
        print(f"[error] no replay result for {args.ticker} as-of {d} "
              "(insufficient history)")
        sys.exit(1)
    print(json.dumps(result, indent=2, default=str))
