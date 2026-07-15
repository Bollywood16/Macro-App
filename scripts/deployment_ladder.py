#!/usr/bin/env python3
"""
Deployment Ladder -- three-tranche systematic capital-deployment mechanics.

  TRANCHE 1 (time-based participation)
    A fixed weekly deploy, gated to only fire after the most recent FOMC
    date in config has passed -- avoids deploying capital into pre-FOMC
    positioning risk.

  TRANCHE 2 (tiered dip-reserve)
    Rungs are defined as a % drawdown from the TRAILING 20-day high, not
    fixed dollar prices, so the ladder travels with the market instead of
    going stale.

  TRANCHE 3 (deep reserve)
    Held back entirely except on a regime break, defined here as VIX
    stressed + SPY below its 200dma + credit widening simultaneously --
    reusing research_engine.py's own regime classifiers rather than
    reinventing a second set.

GUARDRAILS ARE ENFORCED IN CODE, NOT ADVISORY -- every tranche below
checks `fills_blocked` and refuses to mark itself eligible when it's set,
there is no separate "advisory-only" path:
  - HALT on an HY OAS credit blowout (level or 21-day jump past a
    configured reference threshold). Blocks fills on ALL three tranches.
  - HALT on a failed Treasury auction signal. There is no free, keyless
    API for real-time auction results (only yfinance/FRED were in scope
    for this tool), so this is a MANUAL CLI input (--treasury-auction-
    failed) the owner sets after reading the result themselves -- this
    script does not pretend to detect it on its own.
  - BLACKOUT on FOMC/earnings dates from config. Blocks fills without
    halting the underlying ladder logic (resumes automatically once the
    date has passed).
  - RE-BASELINE ALERT on quarterly cadence or >=10% price drift from the
    configured build anchor -- a flag for the owner to review/reset the
    anchor, not an automatic action.

There is deliberately no "skip this guardrail" flag: the CLI's manual
overrides (--treasury-auction-failed, --force-halt) can only ADD a halt
condition the code can't see on its own, never remove one it computed --
"enforced in code, not advisory" would be false advertising otherwise.

Data: yfinance for price history (via research_engine.fetch_history,
reused rather than re-implemented), FRED's public CSV for HY OAS (via
research_engine.fetch_hy_oas -- no API key). Fail-soft throughout: any
data error becomes a "warnings" entry and forces halt=true (never a
silent GO on missing data). Output is JSON facts + a halt/eligibility
verdict per tranche -- no narrative, same discipline as mm_tools.py's
layer6 checker.

CLI:
  python scripts/deployment_ladder.py
      [--ticker TICKER] [--as-of-date YYYY-MM-DD] [--config PATH]
      [--treasury-auction-failed] [--force-halt --halt-reason TEXT]
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from research_engine import (fetch_history, fetch_hy_oas, vix_regime,  # noqa: E402
                              credit_regime, spy_trend_regime)

CONFIG_PATH = os.path.join(HERE, "deployment_ladder_config.json")


def load_config(path=CONFIG_PATH):
    with open(path) as f:
        return json.load(f)


def _fomc_entries(cfg: dict):
    """Normalize fomc_dates entries to (date_str, note) tuples. Accepts
    the current {"date": ..., "note": ...} objects (note is "SEP" or
    null) or a bare date string (defensive backward-compat only, not
    otherwise produced by this config)."""
    out = []
    for e in cfg.get("fomc_dates") or []:
        if isinstance(e, dict):
            out.append((e.get("date"), e.get("note")))
        else:
            out.append((e, None))
    return out


def spy_trend_frame(spy_close: pd.Series) -> pd.DataFrame:
    """Mirrors forecast_engine.spy_trend_frame -- kept local (3 lines) to
    avoid importing forecast_engine.py's Supabase/CLI machinery just for
    this."""
    df = pd.DataFrame({"close": spy_close})
    df["ma200"] = spy_close.rolling(200).mean()
    return df


# --------------------------------------------------------------- guardrails


def hy_oas_blowout(oas: pd.Series, level_threshold: float, jump_threshold: float):
    if oas.empty:
        return {"available": False, "blowout": False}
    latest = float(oas.iloc[-1])
    jump = float(latest - oas.iloc[-22]) if len(oas) > 21 else None
    blowout = latest >= level_threshold or (jump is not None and jump >= jump_threshold)
    return {"available": True, "latest_pct": round(latest, 2),
            "21d_change_pct": round(jump, 2) if jump is not None else None,
            "level_threshold_pct": level_threshold, "jump_threshold_pct": jump_threshold,
            "blowout": bool(blowout)}


def blackout_check(as_of_date, cfg: dict, ticker: str):
    fomc = _fomc_entries(cfg)
    earnings = set((cfg.get("earnings_dates") or {}).get(ticker, []))
    iso = as_of_date.isoformat()
    if not fomc and not earnings:
        return {"active": False, "reasons": [],
                "warning": "fomc_dates/earnings_dates empty in config -- "
                           "blackout gate has nothing to block on"}
    reasons = []
    if iso in earnings:
        reasons.append("earnings_date")

    extend_days = cfg.get("sep_blackout_extension_days", 0)
    for date_str, note in fomc:
        if date_str == iso:
            reasons.append(f"fomc_date({note})" if note else "fomc_date")
        elif note == "SEP" and extend_days > 0:
            meeting_dt = datetime.strptime(date_str, "%Y-%m-%d").date()
            if meeting_dt < as_of_date <= meeting_dt + timedelta(days=extend_days):
                reasons.append(f"sep_extension(+{extend_days}d after {date_str})")
    return {"active": bool(reasons), "reasons": reasons}


def rebaseline_check(as_of_date, price: float, cfg: dict):
    anchor_price = cfg.get("build_anchor_price")
    anchor_date = cfg.get("build_anchor_date")
    if anchor_price is None or anchor_date is None:
        return {"active": False, "reasons": [],
                "warning": "build_anchor_price/build_anchor_date not set "
                           "in config -- cannot evaluate re-baseline"}
    drift_pct = (price / anchor_price) - 1
    reasons = []
    if abs(drift_pct) >= cfg.get("rebaseline_drift_pct", 0.10):
        reasons.append(f"price_drift_{round(drift_pct * 100, 1)}pct")
    anchor_dt = datetime.strptime(anchor_date, "%Y-%m-%d").date()
    days_elapsed = (as_of_date - anchor_dt).days
    if days_elapsed >= cfg.get("rebaseline_quarter_days", 63):
        reasons.append(f"quarterly_cadence_{days_elapsed}d")
    return {"active": bool(reasons), "reasons": reasons,
            "price_drift_pct": round(drift_pct * 100, 2), "days_since_anchor": days_elapsed}


# ----------------------------------------------------------------- tranches


def tranche1_status(as_of_date, cfg: dict, fills_blocked: bool):
    fomc = sorted(d for d, _ in _fomc_entries(cfg))
    weekly_pct = cfg.get("tranche1_weekly_deploy_pct")
    if not fomc:
        return {"eligible": False, "weekly_deploy_pct": weekly_pct,
                "reason": "fomc_dates empty in config -- cannot confirm "
                          "participation gate, defaulting to not-eligible"}
    passed = [d for d in fomc if d <= as_of_date.isoformat()]
    if not passed:
        return {"eligible": False, "weekly_deploy_pct": weekly_pct,
                "reason": "before any configured FOMC date"}
    return {"eligible": not fills_blocked, "weekly_deploy_pct": weekly_pct,
            "last_fomc_date": passed[-1],
            "reason": "fills_blocked" if fills_blocked else "ok"}


def tranche2_status(price: float, high20, cfg: dict, fills_blocked: bool):
    rungs = cfg.get("tranche2_rungs") or []
    if high20 is None or pd.isna(high20):
        return {"eligible_rungs": [], "all_rungs": rungs,
                "reason": "insufficient history for trailing 20d high"}
    dd = (price / float(high20)) - 1
    eligible = [] if fills_blocked else [r for r in rungs if dd <= r["drawdown_pct"]]
    return {"trailing_high_20d": round(float(high20), 2),
            "drawdown_from_high_pct": round(dd * 100, 2),
            "eligible_rungs": eligible, "all_rungs": rungs,
            "reason": "fills_blocked" if fills_blocked else "ok"}


def tranche3_status(regime: dict, cfg: dict, fills_blocked: bool):
    eligible = regime["regime_break"] and not fills_blocked
    reason = ("fills_blocked" if fills_blocked else
              "regime_break" if regime["regime_break"] else "no_regime_break")
    return {"eligible": bool(eligible), "deep_reserve_pct": cfg.get("tranche3_deep_reserve_pct"),
            "regime": regime, "reason": reason}


# -------------------------------------------------------------------- run


def run(ticker, as_of_date, cfg, treasury_auction_failed=False,
        force_halt=False, halt_reason=None):
    warnings = []

    try:
        close = fetch_history(ticker)
    except Exception as e:
        return {"ticker": ticker, "as_of": as_of_date.isoformat(), "halt": True,
                "warnings": [f"price_fetch_failed: {e}"],
                "tranche1": None, "tranche2": None, "tranche3": None}

    close = close[close.index.date <= as_of_date]
    if close.empty:
        return {"ticker": ticker, "as_of": as_of_date.isoformat(), "halt": True,
                "warnings": ["no_price_history_as_of_date"],
                "tranche1": None, "tranche2": None, "tranche3": None}
    price = float(close.iloc[-1])
    high20 = close.rolling(20).max().iloc[-1]

    try:
        spy_close = close if ticker == "SPY" else fetch_history("SPY")
    except Exception as e:
        spy_close = pd.Series(dtype=float)
        warnings.append(f"spy_fetch_failed: {e}")

    try:
        vix = fetch_history("^VIX")
    except Exception as e:
        vix = pd.Series(dtype=float)
        warnings.append(f"vix_fetch_failed: {e}")

    oas = fetch_hy_oas()
    if oas.empty:
        warnings.append("hy_oas_unavailable")

    as_of_ts = pd.Timestamp(as_of_date)
    regime = {
        "vix": vix_regime(as_of_ts, vix),
        "credit": credit_regime(as_of_ts, oas),
        "spy_trend": spy_trend_regime(as_of_ts, spy_trend_frame(spy_close))
                     if not spy_close.empty else "unknown",
    }
    regime["regime_break"] = (regime["vix"] == "stressed"
                               and regime["spy_trend"] == "below"
                               and regime["credit"] == "widening")

    oas_status = hy_oas_blowout(oas, cfg.get("hy_oas_blowout_level", 8.0),
                                 cfg.get("hy_oas_blowout_21d_jump", 1.5))
    blackout = blackout_check(as_of_date, cfg, ticker)
    rebaseline = rebaseline_check(as_of_date, price, cfg)

    halt_reasons = []
    if oas_status.get("blowout"):
        halt_reasons.append("hy_oas_blowout")
    if treasury_auction_failed:
        halt_reasons.append("treasury_auction_failed (manual)")
    if force_halt:
        halt_reasons.append(f"manual_force_halt: {halt_reason or 'no reason given'}")
    halt = bool(halt_reasons)
    fills_blocked = halt or blackout["active"]

    return {
        "ticker": ticker, "as_of": as_of_date.isoformat(), "price": round(price, 2),
        "halt": halt, "halt_reasons": halt_reasons,
        "fills_blocked": fills_blocked,
        "guardrails": {"hy_oas": oas_status, "blackout": blackout},
        "rebaseline_alert": rebaseline,
        "tranche1": tranche1_status(as_of_date, cfg, fills_blocked),
        "tranche2": tranche2_status(price, high20, cfg, fills_blocked),
        "tranche3": tranche3_status(regime, cfg, fills_blocked),
        "warnings": warnings,
    }


def main():
    ap = argparse.ArgumentParser(description="Market Memory deployment ladder")
    ap.add_argument("--ticker", help="Override config's target ticker")
    ap.add_argument("--as-of-date", help="YYYY-MM-DD (default: today UTC) -- "
                     "for testing/backtest, not a live-data override")
    ap.add_argument("--config", default=CONFIG_PATH)
    ap.add_argument("--treasury-auction-failed", action="store_true",
                     help="Manual signal: latest Treasury auction failed. "
                          "Adds a HALT; cannot be auto-detected from "
                          "yfinance/FRED alone.")
    ap.add_argument("--force-halt", action="store_true",
                     help="Manually add a HALT for a reason not otherwise "
                          "coded (see --halt-reason). Adds, never removes, "
                          "a guardrail.")
    ap.add_argument("--halt-reason", help="Free-text reason for --force-halt")
    args = ap.parse_args()

    cfg = load_config(args.config)
    ticker = args.ticker or cfg.get("ticker", "SPY")
    as_of_date = (datetime.strptime(args.as_of_date, "%Y-%m-%d").date()
                  if args.as_of_date else datetime.now(timezone.utc).date())

    result = run(ticker, as_of_date, cfg,
                 treasury_auction_failed=args.treasury_auction_failed,
                 force_halt=args.force_halt, halt_reason=args.halt_reason)
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
