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

NOT the full C4 GitHub Actions workflow by itself -- this is the per-ticker
worker `.github/workflows/replay-backfill.yml`'s matrix jobs each invoke once.
Chunking is by ticker (one job per ticker) and, within a ticker, by an
optional `--start`/`--end` date range so a partial/failed run can be resumed
without redoing already-written dates (idempotency: see --resume below).

Resumability: `--resume` queries the highest `trading_date` already written
for (ticker, voter='forecast') in `forecasts_replay` and starts from the next
trading date after it, rather than re-running the whole range. Cheap (one
indexed query) and avoids depending on any external checkpoint file surviving
across ephemeral GitHub Actions runners.

Run: python3 scripts/replay_backfill.py --ticker SMH --start 2005-01-01
     --end 2025-12-31 [--resume] [--batch-size 500] [--dry-run]
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date as date_type, datetime, timezone

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
                                          "spy_trend": result["regime"][2]}},
            "evidence_json": {
                "recommendation_label": result.get("recommendation_label"),
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

    if resume and not dry_run:
        resp = fe.mm_journal("latest_forecast_replay_date",
                              {"ticker": ticker, "voter": "forecast"})
        latest_str = (resp or {}).get("trading_date")
        if latest_str:
            resumed_start = date_type.fromisoformat(latest_str)
            if resumed_start >= start:
                # Re-does the last written date once (no DB-level
                # upsert-by-date constraint exists to make that free) --
                # accepted duplication in exchange for never risking a gap.
                # A stronger fix (a unique constraint on (ticker,
                # trading_date, horizon_days, voter) + upsert) is a
                # reasonable follow-up, not built here.
                print(f"[resume] last written trading_date={resumed_start}, "
                      f"resuming from there (requested start was {start}; "
                      "that date will be re-written once, not skipped)")
                start = resumed_start

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
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--start", default="2005-01-01")
    ap.add_argument("--end", default="2025-12-31")
    ap.add_argument("--store-root", default=pit.DEFAULT_STORE_ROOT)
    ap.add_argument("--batch-size", type=int, default=BATCH_SIZE_DEFAULT)
    ap.add_argument("--dry-run", action="store_true",
                     help="Compute and print progress only; skip mm-journal writes")
    ap.add_argument("--resume", action="store_true",
                     help="Query forecasts_replay for this ticker's latest "
                          "written trading_date and start from there instead "
                          "of --start")
    args = ap.parse_args()

    result = backfill(
        args.ticker, date_type.fromisoformat(args.start),
        date_type.fromisoformat(args.end), store_root=args.store_root,
        batch_size=args.batch_size, dry_run=args.dry_run, resume=args.resume)
    sys.exit(0 if result["dates_scored"] > 0 or result["dates_total"] == 0 else 1)
