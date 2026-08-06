#!/usr/bin/env python3
"""
replay_backfill.py — Phase C4 backfill driver.

Iterates `replay(ticker, date)` (C2, `scripts/replay.py`) across every trading
date in a range for one ticker, builds `forecasts_replay` rows for both voters
(`forecast` at the replay grid's 4 horizons, `dip_context` at its own native
2), and writes them in batches via mm-journal's `create_forecast_replay_batch`
op (added alongside `supabase/migrations/20260805150000_forecasts_replay_
table.sql` -- see that migration's docstring for why a separate table/op,
not more rows in `forecasts`).

NOT the full C4 GitHub Actions workflow by itself -- `.github/workflows/
replay-backfill.yml`'s matrix jobs each invoke this once per ticker. That
workflow could not actually be dispatched from this environment (GitHub API
returned 403, the token here has no `actions:write`) -- see `--tickers` below
for the fallback this repo's own environment used instead.

Resumability: `--resume` queries the highest `trading_date` already written
for (ticker, voter='forecast') in `forecasts_replay` and starts from the next
trading date AFTER it (not the date itself -- forecasts_replay has no unique
constraint on (ticker, trading_date, horizon_days, voter), so re-processing
the last written date would insert a duplicate set of rows for it, not
overwrite). Cheap (one indexed query) and avoids depending on any external
checkpoint file surviving across ephemeral GitHub Actions runners.

`--tickers a,b,c` (sequential-in-one-process, added this session): why this
exists alongside `--ticker`, and why sequential rather than one subprocess
per ticker. Launching one process per ticker (matching the matrix's own
per-ticker parallelism) was tried first in this repo's own environment --
16 processes on 2 CPU cores. Measured result: severe slowdown from
oversubscription (context-switching/scheduling overhead, not just the naive
1/8-of-a-core-each division), confirmed by killing it and finding each
process had covered only ~150-250 of its ~5,000+ dates after ~15-20 minutes
of wall-clock time -- far worse than 1/16th of a solo run's rate. The
process-count needs to match the core count, not the ticker count.

`--tickers` was ALSO expected to benefit from `pit_store`'s module-level
`as_of()` read cache being shared across tickers within one process (every
ticker's `replay()` call reads the same SPY/QQQ benchmark and VIX/BAA10Y
macro series) -- **measured directly, this benefit is negligible**: XLK
(first ticker in a fresh process) averaged 413.8ms/date over a 300-date
sample; XLF and XLV, run immediately after in the SAME process with
SPY/QQQ/VIX/BAA10Y already warm, averaged 426.7ms and 436.2ms/date --
statistically indistinguishable from XLK's cold rate, not faster. The
shared-disk-read savings only apply to a process's very first call ever;
once a ticker's OWN price series is warm (after its own first date), the
steady-state cost is dominated by compute (`regime_series`/`analog_
positions`/`tech_read`/`dip_context`/`horizon_stats`), which doesn't shrink
from caching at all. **The real reason `--tickers` exists is matching
process count to core count, not the cache.**

Run: python3 scripts/replay_backfill.py --ticker SMH --start 2005-01-01
     --end 2025-12-31 [--resume] [--batch-size 500] [--dry-run]
     python3 scripts/replay_backfill.py --tickers SMH,QQQ,GLD [same flags]
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date as date_type, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd  # noqa: E402

import forecast_engine as fe  # noqa: E402
import pit_store as pit  # noqa: E402
from replay import replay  # noqa: E402

REPLAY_HORIZONS = [1, 5, 20, 60]  # docs/C3_DESIGN.md #4 "Correction" note --
                                    # the replay GRID only, to save backfill
                                    # compute; live keeps all 6.
BATCH_SIZE_DEFAULT = 500


def _trading_dates(ticker: str, start: date_type, end: date_type, store_root: str) -> list:
    full = pit._read_all(pit.PRICES_TABLE, ticker, store_root)
    dates = pd.to_datetime(full["effective_date"]).dt.date
    in_range = sorted(d for d in dates if start <= d <= end)
    return in_range


def _forecast_rows(ticker: str, result: dict) -> list[dict]:
    if not result:
        return []
    as_of_iso = datetime.now(timezone.utc).isoformat()
    rows = []
    for h in REPLAY_HORIZONS:
        ens = (result.get("horizon_rows") or {}).get(h, {}).get("ensemble")
        hc = (result.get("horizon_confidence") or {}).get(h, {})
        rows.append({
            "ticker": ticker, "as_of_ts": as_of_iso,
            "trading_date": result["trading_date"], "scheduler_drift_days": 0,
            "effective_price": round(result["effective_price"], 6),
            "quote_snapshot_id": None,
            "horizon_days": h, "benchmark": fe.BENCHMARK,
            "p_positive": ens["p_positive"] if ens else None,
            "p_beat_benchmark": ens["p_beat_benchmark"] if ens else None,
            "q20": ens["q20"] if ens else None,
            "q50": ens["q50"] if ens else None,
            "q80": ens["q80"] if ens else None,
            "expected_mae": ens["expected_mae"] if ens else None,
            "n_independent": ens["n"] if ens else 0,
            "confidence_score": hc.get("score", 0.0),
            "confidence_label": hc.get("label", "low"),
            "model_version": fe.MODEL_VERSION, "voter": "forecast",
            "regime_model_version": fe.REGIME_MODEL_VERSION,
            "features_json": {"as_of": as_of_iso, "source": "replay",
                               "regime": {"vix": result["regime"][0],
                                          "credit": result["regime"][1],
                                          "spy_trend": result["regime"][2],
                                          "match_depth": result.get("regime_match_depth")}},
            "evidence_json": {
                "recommendation_label": result.get("recommendation_label"),
                "regime_match_depth": result.get("regime_match_depth"),
                "sample_size": {"n": ens["n"] if ens else 0,
                                 "date_start": ens.get("date_start") if ens else None,
                                 "date_end": ens.get("date_end") if ens else None},
            },
        })
    return rows


def _dip_context_rows(ticker: str, result: dict) -> list[dict]:
    dc = (result.get("tearsheet_extras") or {}).get("dip_context")
    if not dc or not dc.get("verdict"):
        return []
    as_of_iso = datetime.now(timezone.utc).isoformat()
    verdict = dc["verdict"]
    rows = []
    for h_str, stats in (dc.get("horizons") or {}).items():
        if not stats:
            continue
        rows.append({
            "ticker": ticker, "as_of_ts": as_of_iso,
            "trading_date": result["trading_date"], "scheduler_drift_days": 0,
            "effective_price": round(result["effective_price"], 6),
            "quote_snapshot_id": None,
            "horizon_days": int(h_str), "benchmark": fe.BENCHMARK,
            "p_positive": stats["p_positive"], "p_beat_benchmark": stats.get("p_beat_benchmark"),
            "q20": stats["q20"], "q50": stats["q50"], "q80": stats["q80"],
            "expected_mae": stats["expected_mae"],
            "n_independent": stats["n"],
            "confidence_score": round((verdict.get("confidence_score") or 0) / 100.0, 4),
            "confidence_label": verdict.get("confidence_label"),
            "model_version": fe.MODEL_VERSION_DIP_CONTEXT, "voter": "dip_context",
            "regime_model_version": fe.REGIME_MODEL_VERSION,
            "features_json": {"as_of": as_of_iso, "source": "replay",
                               "regime": dc.get("regime"),
                               "regime_match_depth": dc.get("regime_match_depth")},
            "evidence_json": {
                "recommendation_label": verdict.get("verdict"),
                "source_model": "dip_context",
                "shadow_size": verdict.get("shadow_size"),
                "caveat": verdict.get("caveat"),
                "regime_match_depth": dc.get("regime_match_depth"),
                "sample_size": {"n": stats["n"], "date_start": stats.get("date_start"),
                                 "date_end": stats.get("date_end")},
            },
        })
    return rows


def _flush(batch: list[dict], dry_run: bool) -> int:
    if not batch:
        return 0
    if dry_run:
        return len(batch)
    resp = fe.mm_journal("create_forecast_replay_batch", {"rows": batch})
    if resp is None or "inserted" not in resp:
        raise RuntimeError(f"create_forecast_replay_batch failed for a "
                            f"{len(batch)}-row batch (see [warn] above)")
    return resp["inserted"]


def backfill(ticker: str, start: date_type, end: date_type,
             store_root: str = pit.DEFAULT_STORE_ROOT,
             batch_size: int = BATCH_SIZE_DEFAULT, dry_run: bool = False,
             resume: bool = False):
    ticker = ticker.upper()

    if resume:
        # A read (query, not a write) -- runs even under --dry-run so
        # --resume --dry-run previews what a real run would pick up without
        # requiring the caller to also skip the one thing that makes
        # --resume meaningful to test. Still needs APP_PASSPHRASE (every
        # mm_journal call does); no-ops gracefully via the same None-safe
        # handling below if it's unset.
        resp = fe.mm_journal("latest_forecast_replay_date",
                              {"ticker": ticker, "voter": "forecast"})
        latest_str = (resp or {}).get("trading_date")
        if latest_str:
            resumed_start = date_type.fromisoformat(latest_str)
            if resumed_start >= start:
                # forecasts_replay has no unique constraint on (ticker,
                # trading_date, horizon_days, voter), so re-processing the
                # last written date would insert a second, duplicate set of
                # rows for it, not overwrite -- excluded via the strict `>`
                # below (_trading_dates' own filter is `start <= d`) rather
                # than accepting that duplication as a known tradeoff.
                # Everything strictly after resumed_start is untouched and
                # gets processed normally; nothing before it is re-read.
                exclusive_start = resumed_start + timedelta(days=1)
                print(f"[resume] last written trading_date={resumed_start}, "
                      f"resuming from the next trading date after it "
                      f"(requested start was {start})")
                start = exclusive_start

    dates = _trading_dates(ticker, start, end, store_root)
    print(f"Backfilling {ticker}: {len(dates)} trading dates, "
          f"{dates[0] if dates else None} -> {dates[-1] if dates else None}"
          f"{' (DRY RUN)' if dry_run else ''}")

    batch: list[dict] = []
    total_rows = 0
    total_dates_scored = 0
    t0 = time.time()
    for i, d in enumerate(dates):
        result = replay(ticker, d, store_root=store_root)
        if result is None:
            continue
        total_dates_scored += 1
        batch += _forecast_rows(ticker, result)
        batch += _dip_context_rows(ticker, result)
        if len(batch) >= batch_size:
            total_rows += _flush(batch, dry_run)
            batch = []
        if (i + 1) % 500 == 0:
            elapsed = time.time() - t0
            rate = (i + 1) / elapsed
            remaining = (len(dates) - (i + 1)) / rate if rate > 0 else float("inf")
            print(f"  [{i + 1}/{len(dates)}] {d} -- {rate:.2f} dates/s, "
                  f"~{remaining / 60:.1f} min remaining")
    total_rows += _flush(batch, dry_run)

    elapsed = time.time() - t0
    print(f"Done: {ticker} -- {total_dates_scored}/{len(dates)} dates scored "
          f"(rest had insufficient history), {total_rows} rows written, "
          f"{elapsed / 60:.1f} min elapsed.")
    return {"ticker": ticker, "dates_scored": total_dates_scored,
            "dates_total": len(dates), "rows_written": total_rows,
            "elapsed_seconds": elapsed}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--ticker", help="Single ticker")
    group.add_argument("--tickers", help="Comma-separated tickers, processed "
                        "SEQUENTIALLY IN THIS ONE PROCESS -- not a shortcut "
                        "for launching one subprocess per ticker. This is what "
                        "makes pit_store's as_of() read cache (module-level,\n"
                        "per-process) shared across tickers, and -- the "
                        "actual reason this exists -- keeps the OS process "
                        "count matched to available cores instead of "
                        "oversubscribing them. See the module docstring's "
                        "'why sequential-in-one-process' note.")
    ap.add_argument("--start", default="2005-01-01")
    ap.add_argument("--end", default="2025-12-31")
    ap.add_argument("--store-root", default=pit.DEFAULT_STORE_ROOT)
    ap.add_argument("--batch-size", type=int, default=BATCH_SIZE_DEFAULT)
    ap.add_argument("--dry-run", action="store_true",
                     help="Compute and print progress only; skip mm-journal writes")
    ap.add_argument("--resume", action="store_true",
                     help="Per ticker: query forecasts_replay for its latest "
                          "written trading_date and start from the next "
                          "trading date after it instead of --start")
    args = ap.parse_args()

    start = date_type.fromisoformat(args.start)
    end = date_type.fromisoformat(args.end)
    tickers = ([args.ticker.upper()] if args.ticker else
               [t.strip().upper() for t in args.tickers.split(",") if t.strip()])

    results = []
    for i, t in enumerate(tickers):
        if len(tickers) > 1:
            print(f"\n=== [{i + 1}/{len(tickers)}] {t} "
                  f"(as_of() cache carries forward from prior tickers "
                  f"in this run) ===", flush=True)
        results.append(backfill(
            t, start, end, store_root=args.store_root,
            batch_size=args.batch_size, dry_run=args.dry_run, resume=args.resume))

    if len(tickers) > 1:
        print("\n=== SUMMARY (this worker) ===")
        for r in results:
            print(f"  {r['ticker']:<6} {r['dates_scored']}/{r['dates_total']} "
                  f"dates scored, {r['rows_written']} rows, "
                  f"{r['elapsed_seconds'] / 60:.1f} min")

    ok = all(r["dates_scored"] > 0 or r["dates_total"] == 0 for r in results)
    sys.exit(0 if ok else 1)
