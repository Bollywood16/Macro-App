"""
tech_read.py — trend / momentum / support-resistance / volume read across a
single OHLCV series, for either the daily or intraday (15m) chart.

Division of labor (same contract as the other engines in this directory):
  * Python computes every number; the LLM/UI only renders `plain` or the
    STRONG/WEAK reclaim pill.
  * Self-contained: RSI is re-derived locally on import failure rather than
    hard-depending on scripts/research_engine across the package boundary
    — but the fallback replicates that exact formula, so numbers agree
    with the rest of the app when both are reachable.
  * Guards for short intraday series: any read that needs more bars than
    the series has degrades to `"available": False` instead of raising or
    fabricating a number from a too-short window (BUILD.md's lookback
    guard for 15m bars, which only carry ~60 days of history).

Input:
  df      OHLCV DataFrame for daily OR intraday (15m) bars; needs 'close',
          optionally 'high'/'low'/'volume'. Index sorted ascending.
  ticker  str, for plain-language sentences.
Output: JSON-serializable dict -> Technicals section + chart series.
"""
from __future__ import annotations
import os
import sys
import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "scripts"))
try:
    from research_engine import rsi as _rsi
except ImportError:
    def _rsi(close: pd.Series, n=14) -> pd.Series:
        d = close.diff()
        up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
        dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
        rs = up / dn
        return 100 - 100 / (1 + rs)

MA_FAST, MA_SLOW = 50, 200
SR_LOOKBACKS = [20, 50, 100]       # support/resistance windows (bars)
VOL_LOOKBACK = 10
RECLAIM_LOOKBACK = 10              # bars searched for a recent MA reclaim cross
RECLAIM_HOLD = 3                   # bars price must hold above to count as "held"


def _macd(close: pd.Series):
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    return macd, signal, macd - signal


def _trend_read(close: pd.Series):
    n = len(close)
    if n < MA_FAST:
        return {"available": False}, None, None
    ma50 = close.rolling(MA_FAST).mean()
    ma200 = close.rolling(MA_SLOW).mean() if n >= MA_SLOW else None
    cur = float(close.iloc[-1])
    above50 = bool(cur >= float(ma50.iloc[-1]))
    above200 = None
    if ma200 is not None and pd.notna(ma200.iloc[-1]):
        above200 = bool(cur >= float(ma200.iloc[-1]))
        golden = bool(float(ma50.iloc[-1]) >= float(ma200.iloc[-1]))
        if above50 and above200 and golden:
            state = "uptrend"
        elif not above50 and not above200 and not golden:
            state = "downtrend"
        else:
            state = "mixed"
    else:
        state = "uptrend" if above50 else "downtrend"
    return {
        "available": True, "state": state,
        "above_ma50": above50, "above_ma200": above200,
        "ma50": round(float(ma50.iloc[-1]), 4),
        "ma200": (round(float(ma200.iloc[-1]), 4)
                  if ma200 is not None and pd.notna(ma200.iloc[-1]) else None),
    }, ma50, ma200


def _momentum_read(close: pd.Series):
    n = len(close)
    if n < 35:  # ~26+9 bars for MACD to be meaningful
        return {"available": False}, None, None
    rsi14 = _rsi(close)
    _, _, hist = _macd(close)
    cur_rsi = float(rsi14.iloc[-1]) if pd.notna(rsi14.iloc[-1]) else None
    cur_hist = float(hist.iloc[-1]) if pd.notna(hist.iloc[-1]) else None
    prior_hist = float(hist.iloc[-2]) if n > 1 and pd.notna(hist.iloc[-2]) else None
    # Divergence-lite: price higher than `lookback` bars ago while RSI is
    # lower than it was then. Flagged as a warning, not asserted as a
    # confirmed divergence — a true divergence needs swing-point detection
    # this module doesn't attempt.
    lookback = min(40, n - 1)
    price_rising = bool(close.iloc[-1] > close.iloc[-lookback])
    rsi_falling = bool(cur_rsi is not None and pd.notna(rsi14.iloc[-lookback])
                        and cur_rsi < float(rsi14.iloc[-lookback]))
    divergence = bool(price_rising and rsi_falling and cur_rsi is not None and cur_rsi < 60)
    return {
        "available": True,
        "rsi14": round(cur_rsi, 1) if cur_rsi is not None else None,
        "macd_hist": round(cur_hist, 4) if cur_hist is not None else None,
        "macd_rising": bool(cur_hist is not None and prior_hist is not None
                             and cur_hist > prior_hist),
        "momentum_divergence": divergence,
    }, rsi14, hist


def _support_resistance(close: pd.Series, high: pd.Series, low: pd.Series):
    n = len(close)
    levels = []
    for w in SR_LOOKBACKS:
        if n < w:
            continue
        levels.append({
            "window": w,
            "resistance": round(float(high.iloc[-w:].max()), 4),
            "support": round(float(low.iloc[-w:].min()), 4),
        })
    return levels


def _volume_read(close: pd.Series, volume, lookback=VOL_LOOKBACK):
    if volume is None or volume.dropna().empty or len(volume) < lookback + 50:
        return {"available": False}
    down_day = close.diff() < 0
    recent_down_vol = volume[-lookback:][down_day[-lookback:]]
    trailing_avg = volume.iloc[-(lookback + 50):-lookback].mean()
    if recent_down_vol.empty or not trailing_avg or pd.isna(trailing_avg):
        return {"available": False}
    ratio = float(recent_down_vol.mean() / trailing_avg)
    return {"available": True, "down_day_vol_ratio": round(ratio, 2)}


def _reclaim_discriminator(close: pd.Series, ma50, volume):
    """STRONG vs WEAK reclaim of the 50-bar average: STRONG needs the cross
    to have happened, price to have HELD above it for RECLAIM_HOLD bars
    without closing back below, and (if volume data exists) the reclaim
    window to have run on above-average volume. A cross with no hold or no
    volume confirmation is WEAK — a reclaim is a claim about durability, so
    absence of confirmation is the honest default, not a coin flip."""
    n = len(close)
    if ma50 is None or n < MA_FAST + RECLAIM_LOOKBACK + RECLAIM_HOLD:
        return {"available": False}
    window = RECLAIM_LOOKBACK + RECLAIM_HOLD
    above = (close >= ma50)
    recent = above.iloc[-window:]
    if not (bool(recent.iloc[0]) is False and bool(recent.iloc[-1]) is True):
        return {"available": True, "reclaimed": False}
    cross_idx = None
    for i in range(1, len(recent)):
        if not recent.iloc[i - 1] and recent.iloc[i]:
            cross_idx = i
    if cross_idx is None:
        return {"available": True, "reclaimed": False}
    held = bool(recent.iloc[cross_idx:].all())
    vol_confirm = None
    if volume is not None and not volume.dropna().empty and len(volume) >= window + 50:
        window_vol = volume.iloc[-window:].iloc[cross_idx:]
        trailing_avg = volume.iloc[-(window + 50):-window].mean()
        vol_confirm = bool(trailing_avg and window_vol.mean() > trailing_avg)
    strong = bool(held and vol_confirm is not False)
    return {"available": True, "reclaimed": True,
            "strength": "STRONG" if strong else "WEAK",
            "held": held, "volume_confirmed": vol_confirm}


def tech_read(df: pd.DataFrame, ticker: str) -> dict:
    close = df["close"]
    high = df["high"] if "high" in df.columns else close
    low = df["low"] if "low" in df.columns else close
    volume = df["volume"] if "volume" in df.columns else None

    trend, ma50, ma200 = _trend_read(close)
    momentum, rsi14, hist = _momentum_read(close)
    sr = _support_resistance(close, high, low)
    vol = _volume_read(close, volume)
    reclaim = _reclaim_discriminator(close, ma50, volume)

    plain = []
    if trend.get("available"):
        sentence = (f"{ticker} is trading "
                    f"{'above' if trend['above_ma50'] else 'below'} its 50-period average")
        if trend.get("above_ma200") is not None:
            sentence += (f" and {'above' if trend['above_ma200'] else 'below'} "
                         "its 200-period average")
        sentence += f" — read as {trend['state']}."
        plain.append(sentence)
    else:
        plain.append("Not enough history yet for a trend read on this series.")
    if momentum.get("available"):
        if momentum["momentum_divergence"]:
            plain.append("Momentum divergence: price is higher than a few weeks ago while "
                          "RSI is lower — a fading-momentum warning, not a reversal call.")
        plain.append(f"RSI-14 is {momentum['rsi14']}; MACD histogram is "
                      f"{'rising' if momentum['macd_rising'] else 'falling'}.")
    if reclaim.get("reclaimed"):
        if reclaim["strength"] == "STRONG":
            plain.append(f"{ticker} STRONGLY reclaimed its 50-period average "
                          "(held, volume-confirmed).")
        else:
            plain.append(f"{ticker} WEAKLY reclaimed its 50-period average "
                          "(not yet confirmed by holding the level or by volume).")
    if vol.get("available"):
        plain.append(f"Volume on down days recently is running "
                      f"~{vol['down_day_vol_ratio']:.1f}x the trailing average.")

    def _series(s):
        return [round(float(v), 4) if pd.notna(v) else None for v in s] if s is not None else None

    chart_series = {
        "dates": [str(i) for i in close.index],
        "close": _series(close),
        "ma50": _series(ma50),
        "ma200": _series(ma200),
        "rsi14": _series(rsi14),
        "macd_hist": _series(hist),
        "volume": ([float(v) if pd.notna(v) else None for v in volume]
                   if volume is not None else None),
    }

    return {
        "ticker": ticker,
        "trend": trend,
        "momentum": momentum,
        "support_resistance": sr,
        "volume": vol,
        "reclaim": reclaim,
        "plain": plain,
        "chart_series": chart_series,
    }


def to_ballot(result: dict) -> dict:
    """ONE voter for the vote-aggregation engine (agreement_engine, module
    D). NOT a verdict. Ships calibrated=False / weight 0 until scored
    against matured outcomes, same as every other secondary voter."""
    trend = result.get("trend") or {}
    mom = result.get("momentum") or {}
    state = trend.get("state")
    vote = {"uptrend": "BUY", "downtrend": "AVOID"}.get(state, "WAIT")
    if mom.get("available"):
        agree = ((state == "uptrend" and mom.get("macd_rising"))
                 or (state == "downtrend" and not mom.get("macd_rising")))
        confidence = 0.55 if agree else 0.4
    else:
        confidence = 0.4
    return {"voter": "tech_read", "vote": vote, "raw_confidence": confidence,
            "calibrated": False, "weight_until_calibrated": 0.0,
            "independent_n": 1,
            "rationale": "; ".join(result.get("plain", [])[:2])}
