#!/usr/bin/env python3
"""
Daily Opportunity Scanner — Market Memory M4.

Reads back today's already-persisted forecasts (written by the
forecast-engine.yml cron, which this script does NOT duplicate — see
below) and turns them into "Today's Setups": a confidence-gated, ranked
shortlist of the ETFs actually worth looking at, per spec 8.

WHY THIS DOESN'T PERSIST ITS OWN FORECAST ROWS
forecast_engine.py's unconditional daily batch (all ~19 tickers, every
weekday) already ran and persisted forecasts by the time this workflow
fires (scheduled ~25 min later). Recomputing and re-persisting here would
double-write Supabase for no benefit and would risk numbers drifting from
what the Forecast/Journal UI already shows. Instead this script:
  1. Recomputes only the lightweight trigger features (needs price history
     anyway — no way around a second yfinance pull, same redundancy every
     engine in this repo already accepts).
  2. Reads back today's forecast via the existing get_latest_forecast op.
  3. Gates and ranks what's already there.
A forecast whose as_of_ts isn't from today (stale, or the batch cron
hasn't run/failed) is skipped, not silently reused — see `skipped` in the
digest.

TRIGGERS (spec 8.2 subset named in MASTER_AGENT_PROMPT.md's M4 milestone)
  return_percentile        today's 1-day return <= the ticker's own
                            trailing 5th percentile
  rsi_extreme               RSI-14 <=30 or >=70
  drawdown_threshold        drawdown from the 252d high <=-15%
  relative_underperformance 63d return vs SPY <=-5 percentage points
  regime_transition         (vix, credit, spy_trend) regime differs from
                             5 trading days ago

GATE
  Skip tickers whose latest forecast's evidence_json.recommendation_label
  is "no_reliable_signal" — the same no-signal gate forecast_engine.py
  already computes; this script doesn't re-derive it.

  regime_transition is a special case: vix/credit/spy_trend are shared
  market-wide inputs (same regime series reindexed onto every ticker's own
  trading calendar), so on a day the regime actually flips, EVERY ticker in
  the universe shows it — it's not specific to any one ETF. Treated alone
  it would flood the scanner with the entire universe rather than surface
  anything differentiating. So regime_transition is recorded for context
  but never qualifies a ticker by itself — at least one idiosyncratic
  trigger (return_percentile / rsi_extreme / drawdown_threshold /
  relative_underperformance) must also be active.

RANKING (spec 8.3, v1 placeholder weights — same "computed, not vibed,
but honestly labeled" spirit as the confidence shrinkage formula in
forecast_engine.py; real weight-fitting is a later milestone)
  opportunity_score = expected_excess_return
                       - LAMBDA_DOWNSIDE * |expected_mae|
                       - LAMBDA_UNCERTAINTY * (q80 - q20)
                       - LAMBDA_COST * TRANSACTION_COST_ESTIMATE
                       - LAMBDA_DRIFT * distribution_shift
                       + LAMBDA_CALIBRATION * confidence_score
  expected_excess_return comes from evidence_json.ensemble_mean_excess_return
  (a small additive field forecast_engine.py now also computes).

Zero setups on a given day is a valid, expected output (spec 8.3: activity
is not success). Output: data/scanner_digest.json (committed, no
passphrase needed to read it — same pattern as research/rotation digests).
"""

import json
import os
import sys
from datetime import datetime, timezone

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import forecast_engine as fe  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DIGEST_PATH = os.path.join(ROOT, "data", "scanner_digest.json")

TRIGGERS = {
    "return_percentile": "1-day return at/below the ticker's own trailing 5th percentile",
    "rsi_extreme": "RSI-14 at or beyond 30 (oversold) / 70 (overbought)",
    "drawdown_threshold": "drawdown from the 252-day high at/below -15%",
    "relative_underperformance": "63-day return vs SPY at/below -5 percentage points",
    "regime_transition": "vix/credit/SPY-trend regime differs from 5 trading days ago",
}
RETURN_PCTL_TRIGGER = 0.05
RSI_LOW, RSI_HIGH = 30, 70
DRAWDOWN_TRIGGER = -0.15
REL_UNDERPERF_TRIGGER = -0.05
REGIME_LOOKBACK = 5

LAMBDA_DOWNSIDE = 1.0
LAMBDA_UNCERTAINTY = 0.5
LAMBDA_COST = 1.0
TRANSACTION_COST_ESTIMATE = 0.0015
LAMBDA_DRIFT = 0.15
LAMBDA_CALIBRATION = 0.2

PRIMARY_HORIZON = 5

# --------------------------------------------------------------- triggers


def active_triggers(df: pd.DataFrame, regime_tuples):
    active = []
    ret_series = df["ret_1d"].dropna()
    if len(ret_series) > 20:
        current_ret = ret_series.iloc[-1]
        pctl = float((ret_series <= current_ret).mean())
        if pctl <= RETURN_PCTL_TRIGGER:
            active.append("return_percentile")

    rsi = df["rsi14"].iloc[-1]
    if pd.notna(rsi) and (rsi <= RSI_LOW or rsi >= RSI_HIGH):
        active.append("rsi_extreme")

    dd = df["drawdown_252"].iloc[-1]
    if pd.notna(dd) and dd <= DRAWDOWN_TRIGGER:
        active.append("drawdown_threshold")

    rel = df["rel_spy_63d"].iloc[-1]
    if pd.notna(rel) and rel <= REL_UNDERPERF_TRIGGER:
        active.append("relative_underperformance")

    if len(regime_tuples) > REGIME_LOOKBACK:
        if regime_tuples[-1] != regime_tuples[-1 - REGIME_LOOKBACK]:
            active.append("regime_transition")

    return active


# ----------------------------------------------------------------- ranking


def opportunity_score(row, ev, ft):
    expected_excess = ev.get("ensemble_mean_excess_return") or 0.0
    q20, q80 = row.get("q20"), row.get("q80")
    spread = (q80 - q20) if (q20 is not None and q80 is not None) else 0.0
    mae = row.get("expected_mae") or 0.0
    conf = row.get("confidence_score") or 0.0
    drift = 1.0 if ft.get("distribution_shift") else 0.0
    score = (expected_excess
             - LAMBDA_DOWNSIDE * abs(mae)
             - LAMBDA_UNCERTAINTY * spread
             - LAMBDA_COST * TRANSACTION_COST_ESTIMATE
             - LAMBDA_DRIFT * drift
             + LAMBDA_CALIBRATION * conf)
    return round(score, 5)


# ----------------------------------------------------------------- per-asset


def scan_ticker(asset, universe_prices, spy_close, spy_trend_df, vix, oas, today_utc):
    ticker = asset["ticker"]
    close = universe_prices.get(ticker)
    if close is None or len(close) < 260:
        return None

    df = fe.build_feature_frame(close, spy_close)
    regime_tuples = fe.regime_series(df.index, vix, oas, spy_trend_df)
    triggers = active_triggers(df, regime_tuples)
    idiosyncratic = [t for t in triggers if t != "regime_transition"]
    if not idiosyncratic:
        return None

    resp = fe.mm_journal("get_latest_forecast", {"ticker": ticker})
    rows = (resp or {}).get("forecasts") or []
    if not rows:
        return {"ticker": ticker, "skip_reason": "no_forecast_available"}

    as_of = rows[0].get("as_of_ts") or ""
    if as_of[:10] != today_utc:
        return {"ticker": ticker, "skip_reason": "stale_forecast", "as_of_ts": as_of}

    row = next((r for r in rows if r.get("horizon_days") == PRIMARY_HORIZON), rows[0])
    ev = row.get("evidence_json") or {}
    ft = row.get("features_json") or {}
    if ev.get("recommendation_label") == "no_reliable_signal":
        return {"ticker": ticker, "triggers": triggers, "gated_out": True}

    return {
        "ticker": ticker, "label": asset["label"], "triggers": triggers,
        "opportunity_score": opportunity_score(row, ev, ft), "row": row,
        "ev": ev, "ft": ft,
    }


def format_setup(s):
    row, ev, ft = s["row"], s["ev"], s["ft"]
    return {
        "ticker": s["ticker"], "label": s["label"], "triggers_active": s["triggers"],
        "forecast_id": row.get("id"),
        "opportunity_score": s["opportunity_score"],
        "recommendation_label": ev.get("recommendation_label"),
        "model_action": ev.get("model_action"),
        "why_it_triggered": ev.get("why_it_triggered"),
        "invalidation_risks": ev.get("invalidation_risks"),
        "warnings": ev.get("warnings"),
        "regime": ft.get("regime"),
        "p_positive": row.get("p_positive"), "p_beat_benchmark": row.get("p_beat_benchmark"),
        "q20": row.get("q20"), "q50": row.get("q50"), "q80": row.get("q80"),
        "expected_mae": row.get("expected_mae"), "n_independent": row.get("n_independent"),
        "confidence_score": row.get("confidence_score"),
        "confidence_label": row.get("confidence_label"),
        "horizon_days": row.get("horizon_days"), "as_of_ts": row.get("as_of_ts"),
        "effective_price": row.get("effective_price"),
    }


# -------------------------------------------------------------------- main


def main():
    universe = fe.load_universe()
    spy_close = fe.re_engine.fetch_history("SPY")
    spy_trend_df = fe.spy_trend_frame(spy_close)
    try:
        vix = fe.re_engine.fetch_history("^VIX")
    except Exception:
        vix = pd.Series(dtype=float)
    oas = fe.re_engine.fetch_hy_oas()

    universe_prices = {"SPY": spy_close}
    for a in universe:
        t = a["ticker"]
        try:
            universe_prices[t] = fe.re_engine.fetch_history(t)
        except Exception as e:
            print(f"[warn] skipping {t}: {e}")

    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    setups, skipped, gated = [], [], []
    for a in universe:
        r = scan_ticker(a, universe_prices, spy_close, spy_trend_df, vix, oas, today_utc)
        if r is None:
            continue
        if r.get("skip_reason"):
            skipped.append(r)
        elif r.get("gated_out"):
            gated.append({"ticker": r["ticker"], "triggers": r["triggers"]})
        else:
            setups.append(r)

    setups.sort(key=lambda s: -s["opportunity_score"])

    digest = {
        "meta": {
            "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "triggers": TRIGGERS,
            "ranking_weights": {
                "lambda_downside": LAMBDA_DOWNSIDE,
                "lambda_uncertainty": LAMBDA_UNCERTAINTY,
                "lambda_cost": LAMBDA_COST,
                "transaction_cost_estimate": TRANSACTION_COST_ESTIMATE,
                "lambda_drift": LAMBDA_DRIFT,
                "lambda_calibration": LAMBDA_CALIBRATION,
            },
            "caveats": [
                "This script does not compute forecasts — it reads back "
                "today's forecast-engine.yml batch output and ranks it. A "
                "ticker with an active trigger but no forecast dated today "
                "(batch didn't run yet, or failed) is skipped, never "
                "silently backfilled with a stale number — see 'skipped'.",
                "opportunity_score weights are v1 placeholders, not fitted "
                "— treat the ranking as a rough ordering, not a precise "
                "economic estimate.",
                "Zero setups on a given day is expected, not an error: "
                "activity is not success (spec 8.3).",
                "Gated-out tickers (an active trigger, but the forecast's "
                "own no-signal gate rejected it) are listed separately, "
                "not silently dropped — see 'gated_out'.",
            ],
        },
        "setups": [format_setup(s) for s in setups],
        "gated_out": gated,
        "skipped": skipped,
        "scanned_universe_count": len(universe),
        "setups_count": len(setups),
    }

    os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
    with open(DIGEST_PATH, "w") as f:
        json.dump(digest, f)

    print(f"scanned={len(universe)} setups={len(setups)} "
          f"gated_out={len(gated)} skipped={len(skipped)}")
    for s in setups[:10]:
        print(f"  #{setups.index(s)+1} {s['ticker']}: score={s['opportunity_score']} "
              f"triggers={s['triggers']}")


if __name__ == "__main__":
    sys.exit(main())
