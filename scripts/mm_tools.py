#!/usr/bin/env python3
"""
mm_tools.py -- Market Memory execution-mechanics toolkit.

Two independent subcommands, both mechanical (no LLM, no vibes, no
narrative -- JSON facts and a verdict only):

  layer6   Reclaim-gate checker for intraday setups. GO only if, on the
           most recently CLOSED bar: close > session VWAP AND bar volume
           >= the prior bar's volume AND MACD histogram > 0. All three
           gates must pass; any miss is NO-GO. Never evaluates the
           currently-forming (open) bar.
  lookup   Untracked-ticker proxy lookup via a curated PROXY_MAP. Output
           is always labeled UNTRACKED -- this never produces a forecast
           or confidence score, only a nearest-tracked-proxy pointer with
           a caveat.

ALPACA UPGRADE PATH: fetch_intraday() is the only function that talks to
a market-data vendor. Swap its body for an Alpaca bars call that returns
the same shape (DataFrame indexed by tz-aware bar timestamp, columns
open/high/low/close/volume) and every gate/verdict function below is
unaffected -- they only consume that DataFrame.

CLI:
  python scripts/mm_tools.py layer6 --ticker SMH [--interval 15m]
  python scripts/mm_tools.py lookup --ticker SBIO
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from research_engine import rsi  # noqa: E402  (reuse, don't re-derive)

# --------------------------------------------------------------- proxy map

# Untracked tickers this app has no history/forecast pipeline for, mapped
# to the nearest tracked asset for CONTEXT ONLY. Never used to synthesize
# a forecast for the untracked ticker itself -- see lookup()'s docstring.
PROXY_MAP = {
    "SBIO": {"proxy": "XBI", "caveat":
             "Sub-sector biotech basket proxied by the broader biotech "
             "ETF XBI -- sector/market-cap composition differs, treat "
             "as directional context only, not a substitute forecast."},
    "SOXL": {"proxy": "SMH", "caveat":
             "3x LEVERAGED semiconductor ETF proxied by unleveraged SMH. "
             "Daily-reset leverage compounds -- SOXL's multi-day return "
             "is NOT 3x SMH's multi-day return, and it decays in choppy/ "
             "sideways markets even if SMH is flat. Directional context "
             "only."},
    "KORU": {"proxy": "EWY", "caveat":
             "3x LEVERAGED Korea ETF proxied by unleveraged EWY. "
             "Daily-reset leverage compounds -- KORU's multi-day return "
             "is NOT 3x EWY's multi-day return, and it decays in choppy/ "
             "sideways markets even if EWY is flat. Directional context "
             "only."},
}

# ---------------------------------------------------------------- fetching


def fetch_intraday(ticker: str, interval: str = "15m", lookback_days: int = 5) -> pd.DataFrame:
    """yfinance intraday bars -- the ALPACA UPGRADE PATH boundary. Replace
    this function's body only; return the same shape (tz-aware index,
    columns open/high/low/close/volume, oldest-to-newest) and nothing
    downstream needs to change.
    """
    import yfinance as yf
    df = yf.download(ticker, period=f"{lookback_days}d", interval=interval,
                      auto_adjust=False, progress=False)
    if df is None or df.empty:
        raise RuntimeError(f"No intraday data for {ticker} @ {interval}")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.lower)
    df.index = pd.to_datetime(df.index)
    return df[["open", "high", "low", "close", "volume"]].dropna()


_INTERVAL_MINUTES = {"1m": 1, "2m": 2, "5m": 5, "15m": 15, "30m": 30,
                      "60m": 60, "90m": 90, "1h": 60}


def _interval_timedelta(interval: str) -> pd.Timedelta:
    minutes = _INTERVAL_MINUTES.get(interval)
    if minutes is None:
        raise ValueError(f"Unsupported interval for gate evaluation: {interval}")
    return pd.Timedelta(minutes=minutes)


def drop_open_bar(df: pd.DataFrame, interval: str) -> pd.DataFrame:
    """Drop the last row if it's still the currently-forming bar (its bar
    window hasn't closed as of now). Bars are indexed by their START
    time, so a bar is closed once now >= start + interval."""
    if df.empty:
        return df
    delta = _interval_timedelta(interval)
    now = pd.Timestamp.now(tz=df.index.tz)
    bar_end = df.index[-1] + delta
    return df.iloc[:-1] if now < bar_end else df


# ------------------------------------------------------------ indicators


def session_vwap(df: pd.DataFrame) -> pd.Series:
    """Cumulative typical-price VWAP, reset at the start of each session
    (calendar date of the bar index) -- not a running VWAP across days."""
    typical = (df["high"] + df["low"] + df["close"]) / 3
    tp_vol = typical * df["volume"]
    day = df.index.date
    cum_tp_vol = tp_vol.groupby(day).cumsum()
    cum_vol = df["volume"].groupby(day).cumsum()
    return cum_tp_vol / cum_vol


def macd_histogram(close: pd.Series) -> pd.Series:
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    return macd - signal


# --------------------------------------------------------------- layer6


def run_layer6(ticker: str, interval: str = "15m") -> dict:
    base = {"ticker": ticker.upper(), "interval": interval,
            "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}
    try:
        df = fetch_intraday(ticker, interval)
    except Exception as e:
        return {**base, "verdict": "NO-GO", "facts": {}, "gates": {},
                "warnings": [f"data_fetch_failed: {e}"]}

    try:
        df = drop_open_bar(df, interval)
    except ValueError as e:
        return {**base, "verdict": "NO-GO", "facts": {}, "gates": {},
                "warnings": [str(e)]}

    if len(df) < 2:
        return {**base, "verdict": "NO-GO", "facts": {}, "gates": {},
                "warnings": ["insufficient_closed_bars (need >=2 to compare volume)"]}

    vwap = session_vwap(df)
    hist = macd_histogram(df["close"])
    rsi14 = rsi(df["close"], 14)

    bar, prior = df.iloc[-1], df.iloc[-2]
    bar_vwap = float(vwap.iloc[-1])
    bar_hist = float(hist.iloc[-1])
    bar_rsi = float(rsi14.iloc[-1]) if pd.notna(rsi14.iloc[-1]) else None

    prior_n = df.iloc[-21:-1] if len(df) > 21 else df.iloc[:-1]
    avg_prior_vol = float(prior_n["volume"].mean()) if not prior_n.empty else None

    gates = {
        "close_above_vwap": bool(bar["close"] > bar_vwap),
        "volume_ge_prior_bar": bool(bar["volume"] >= prior["volume"]),
        "macd_histogram_positive": bool(bar_hist > 0),
    }
    verdict = "GO" if all(gates.values()) else "NO-GO"

    facts = {
        "bar_ts": bar.name.isoformat(),
        "close": round(float(bar["close"]), 4),
        "session_vwap": round(bar_vwap, 4),
        "bar_volume": int(bar["volume"]),
        "prior_bar_volume": int(prior["volume"]),
        "relative_volume_vs_trailing_avg": (
            round(float(bar["volume"]) / avg_prior_vol, 3)
            if avg_prior_vol else None),
        "macd_histogram": round(bar_hist, 5),
        "rsi14": round(bar_rsi, 2) if bar_rsi is not None else None,
    }

    warnings = []
    if len(df) < 27:
        warnings.append("short_history: MACD/RSI may still be settling "
                         "(fewer than 27 closed bars fetched)")

    return {**base, "verdict": verdict, "gates": gates, "facts": facts, "warnings": warnings}


# ---------------------------------------------------------------- lookup


def lookup(ticker: str) -> dict:
    """Untracked-ticker proxy lookup. Status is ALWAYS "UNTRACKED" -- a
    proxy match gives directional context, never a forecast/confidence
    score for the ticker itself (MASTER_AGENT_PROMPT.md #3: the LLM/tools
    layer never invents confidence for something it hasn't modeled)."""
    t = ticker.upper()
    entry = PROXY_MAP.get(t)
    if not entry:
        return {"ticker": t, "status": "UNTRACKED", "proxy": None,
                "caveat": "No curated proxy mapping exists for this ticker. "
                          "No forecast or confidence score can be generated.",
                "warnings": ["unmapped_ticker"]}
    return {"ticker": t, "status": "UNTRACKED", "proxy": entry["proxy"],
            "caveat": entry["caveat"], "warnings": []}


# ------------------------------------------------------------------ main


def main():
    ap = argparse.ArgumentParser(description="Market Memory execution-mechanics tools")
    sub = ap.add_subparsers(dest="command", required=True)

    p6 = sub.add_parser("layer6", help="Layer 6 mechanical reclaim gate checker")
    p6.add_argument("--ticker", required=True)
    p6.add_argument("--interval", default="15m")

    pl = sub.add_parser("lookup", help="Untracked-ticker proxy lookup")
    pl.add_argument("--ticker", required=True)

    args = ap.parse_args()
    result = (run_layer6(args.ticker, args.interval) if args.command == "layer6"
              else lookup(args.ticker))
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
