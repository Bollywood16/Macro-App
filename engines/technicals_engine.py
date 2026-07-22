"""
technicals_engine.py — Market Memory technicals layer.

Design contract (matches the app's core rule: Python/SQL computes, the LLM interprets):
this module emits STRUCTURED FACTS and PLAIN-LANGUAGE CALLOUTS for the handoff bundle.
It never emits a verdict. Verdicts are the tear sheet's job (confidence-gated) and,
optionally, the investment-committee vote aggregator's job — for which this module
supplies ONE calibratable ballot via to_committee_ballot().

Inputs are DataFrames the app already has (yfinance-shaped): columns
['open','high','low','close','volume'] indexed by datetime. Daily is required;
an optional intraday frame (e.g. today's 15m bars) unlocks the Flush->Stall->Reclaim gate.

Nothing here fetches data or makes network calls — deterministic given its inputs,
so it is unit-testable and its output is reproducible in the immutable journal.
"""

from __future__ import annotations
from dataclasses import dataclass, asdict, field
from typing import Optional
import numpy as np
import pandas as pd


# ---------- primitive indicators ----------

def _ema(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, adjust=False).mean()

def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    # Wilder smoothing
    avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50.0)

def macd(close: pd.Series, fast=12, slow=26, signal=9):
    line = _ema(close, fast) - _ema(close, slow)
    sig = _ema(line, signal)
    hist = line - sig
    return line, sig, hist

def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([(h - l), (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, adjust=False).mean()


# ---------- volume regime (up/down-day relative volume) ----------

@dataclass
class VolumeRegime:
    trailing_avg_volume: float
    up_day_relvol: float          # avg vol on up-close days / trailing avg
    down_day_relvol: float        # avg vol on down-close days / trailing avg
    today_relvol: float           # today's vol / trailing avg
    today_is_up: bool
    signature: str                # 'accumulation' | 'distribution' | 'neutral'

def volume_regime(df: pd.DataFrame, lookback: int = 20) -> VolumeRegime:
    w = df.tail(lookback)
    up = w[w["close"] >= w["open"]]
    dn = w[w["close"] < w["open"]]
    avg = float(w["volume"].mean())
    up_rv = float(up["volume"].mean() / avg) if len(up) and avg else 0.0
    dn_rv = float(dn["volume"].mean() / avg) if len(dn) and avg else 0.0
    today = df.iloc[-1]
    today_rv = (float(today["volume"] / avg)
                if avg and pd.notna(today["volume"]) else 0.0)
    today_up = bool(today["close"] >= today["open"])
    # signature: which side is volume leaning on, with a dead-band to avoid over-reading noise
    if up_rv >= dn_rv * 1.15 and up_rv >= 1.0:
        sig = "accumulation"
    elif dn_rv >= up_rv * 1.15 and dn_rv >= 1.0:
        sig = "distribution"
    else:
        sig = "neutral"
    return VolumeRegime(round(avg, 1), round(up_rv, 2), round(dn_rv, 2),
                        round(today_rv, 2), today_up, sig)


# ---------- volume-by-price: overhead supply / support shelves ----------

@dataclass
class Shelf:
    price_low: float
    price_high: float
    center: float
    volume_share: float           # fraction of total volume that traded in this band
    side: str                     # 'overhead' | 'underfoot'
    distance_pct: float           # signed % from current price to shelf center

def supply_shelves(df: pd.DataFrame, lookback: int = 90, bins: int = 24,
                   hvn_quantile: float = 0.80, max_shelves: int = 3) -> list[Shelf]:
    """Volume-by-price histogram. High-volume nodes above spot = resistance (overhead
    supply); below spot = support (demand). Volume for each bar is spread uniformly
    across [low, high] so wide bars don't get mis-assigned to a single tick."""
    w = df.tail(lookback)
    lo, hi = float(w["low"].min()), float(w["high"].max())
    if hi <= lo:
        return []
    edges = np.linspace(lo, hi, bins + 1)
    vol = np.zeros(bins)
    for _, r in w.iterrows():
        if pd.isna(r["volume"]):
            continue  # e.g. today's synthetic manual-override bar carries no real volume
        b_lo = max(r["low"], lo); b_hi = min(r["high"], hi)
        if b_hi <= b_lo:
            idx = min(int((r["close"] - lo) / (hi - lo) * bins), bins - 1)
            vol[idx] += r["volume"]; continue
        first = int((b_lo - lo) / (hi - lo) * bins)
        last = min(int((b_hi - lo) / (hi - lo) * bins), bins - 1)
        span = last - first + 1
        vol[first:last + 1] += r["volume"] / span
    total = vol.sum()
    thresh = np.quantile(vol, hvn_quantile)
    spot = float(df["close"].iloc[-1])
    shelves = []
    for i in range(bins):
        if vol[i] < thresh:
            continue
        c = (edges[i] + edges[i + 1]) / 2
        shelves.append(Shelf(
            round(edges[i], 2), round(edges[i + 1], 2), round(c, 2),
            round(vol[i] / total, 3),
            "overhead" if c > spot else "underfoot",
            round((c - spot) / spot * 100, 1)))
    # keep the heaviest nodes, nearest-first within each side
    shelves.sort(key=lambda s: (-s.volume_share))
    return shelves[:max_shelves]


# ---------- Flush -> Stall -> Reclaim gate (intraday) ----------

@dataclass
class ReclaimGate:
    available: bool
    flush: bool = False
    stall: bool = False
    reclaim: bool = False
    strong: Optional[bool] = None     # STRONG (vol+momentum agree with price) vs WEAK
    stage: str = "n/a"                # 'none'|'flush'|'stall'|'reclaim'
    note: str = ""

def reclaim_gate(intraday: Optional[pd.DataFrame], daily_atr: float,
                 flush_atr_mult: float = 2.0, flush_relvol: float = 2.0,
                 stall_bars: int = 4) -> ReclaimGate:
    if intraday is None or len(intraday) < stall_bars + 2:
        return ReclaimGate(available=False, note="needs intraday bars (e.g. 15m) for today")
    d = intraday.copy()
    d["vwap"] = (d["close"] * d["volume"]).cumsum() / d["volume"].cumsum().replace(0, np.nan)
    avg_v = d["volume"].mean()
    rng = d["high"] - d["low"]
    flush_mask = (rng > flush_atr_mult * daily_atr) & (d["volume"] > flush_relvol * avg_v)
    if not flush_mask.any():
        return ReclaimGate(available=True, stage="none", note="no flush candle today")
    fi = int(np.argmax(flush_mask.values))
    flush_low = d["low"].iloc[fi:fi + 1].min()
    after = d.iloc[fi + 1:]
    stall = bool(len(after) >= stall_bars and (after["low"].iloc[:stall_bars] >= flush_low).all())
    reclaim = bool(len(after) and after["close"].iloc[-1] > after["vwap"].iloc[-1])
    vol_rising = bool(len(after) >= 2 and after["volume"].iloc[-1] > after["volume"].iloc[-2])
    mom_up = bool(len(after) >= 2 and after["close"].iloc[-1] > after["close"].iloc[-2])
    strong = (vol_rising and mom_up) if reclaim else None
    stage = "reclaim" if reclaim else ("stall" if stall else "flush")
    return ReclaimGate(True, True, stall, reclaim, strong, stage,
                       "STRONG reclaim (vol+momentum agree)" if strong
                       else "WEAK reclaim (vol/momentum diverge)" if reclaim is True and strong is False
                       else f"reached {stage}")


# ---------- top-level read + handoff serialization ----------

@dataclass
class TechnicalsRead:
    as_of: str
    spot: float
    rsi14: float
    macd_line: float
    macd_signal: float
    macd_hist: float
    macd_cross: str               # 'bullish'|'bearish'|'none' in last 2 bars
    volume: VolumeRegime
    shelves: list
    gate: ReclaimGate
    plain_language: list = field(default_factory=list)

    def to_handoff_block(self) -> dict:
        d = asdict(self)
        d["shelves"] = [asdict(s) if not isinstance(s, dict) else s for s in self.shelves]
        return {"technicals": d}

    def active_gate_count(self) -> int:
        """Count of active technical gates for the confidence penalty
        (upgrade doc §3): accumulation, intraday reclaim, clear-of-overhead-
        supply. Each True gate is one more condition thinning the effective
        sample behind the read, so it feeds the conjunction-depth penalty
        rather than inflating confidence for free."""
        overhead = [s for s in self.shelves if getattr(s, "side", None) == "overhead"]
        clear_of_supply = not overhead or min(s.distance_pct for s in overhead) > 6
        gates = [
            self.volume.signature == "accumulation",
            self.gate.stage == "reclaim",
            clear_of_supply,
        ]
        return sum(1 for g in gates if g)

    def to_committee_ballot(self) -> dict:
        """ONE voter for the vote-aggregation engine. NOT a verdict.
        MUST be scored against matured outcomes before its weight is trusted;
        until then the aggregator should treat this ballot as weight 0 (advisory only)."""
        overhead = [s for s in self.shelves if getattr(s, "side", None) == "overhead"]
        near_supply = overhead and min(s.distance_pct for s in overhead) < 6
        if self.gate.reclaim and self.gate.strong and self.volume.signature == "accumulation":
            vote, conf = "BUY", 0.6
        elif near_supply and self.volume.signature != "accumulation":
            vote, conf = "WAIT", 0.5
        elif self.volume.signature == "distribution":
            vote, conf = "AVOID", 0.45
        else:
            vote, conf = "WAIT", 0.4
        return {"voter": "technicals", "vote": vote, "raw_confidence": conf,
                "calibrated": False, "weight_until_calibrated": 0.0,
                "independent_n": 1,
                "rationale": "; ".join(self.plain_language[:2])}


def _cross_state(line: pd.Series, sig: pd.Series) -> str:
    if len(line) < 2:
        return "none"
    prev, now = line.iloc[-2] - sig.iloc[-2], line.iloc[-1] - sig.iloc[-1]
    if prev <= 0 < now:
        return "bullish"
    if prev >= 0 > now:
        return "bearish"
    return "none"


def analyze(daily: pd.DataFrame, intraday: Optional[pd.DataFrame] = None,
            as_of: str = "") -> TechnicalsRead:
    daily = daily.copy()
    close = daily["close"]
    r = rsi(close)
    ml, sl, hl = macd(close)
    a = atr(daily)
    vr = volume_regime(daily)
    shelves = supply_shelves(daily)
    gate = reclaim_gate(intraday, float(a.iloc[-1]))
    spot = float(close.iloc[-1])

    # plain-language callouts (layman's terms — the app's stated UX requirement)
    pl = []
    pl.append({
        "accumulation": "Buyers are leaning in: up-days are trading heavier than down-days.",
        "distribution": "Sellers are leaning in: down-days are trading heavier than up-days.",
        "neutral": "Volume is even between up and down days — no clear hand in control.",
    }[vr.signature])
    overhead = sorted([s for s in shelves if s.side == "overhead"], key=lambda s: s.distance_pct)
    if overhead:
        nxt = overhead[0]
        pl.append(f"First overhead supply is ~{nxt.distance_pct:.0f}% up, around "
                  f"{nxt.price_low:.0f}-{nxt.price_high:.0f} — heavy prior trading there is likely resistance.")
    rr = r.iloc[-1]
    pl.append(f"Momentum (RSI {rr:.0f}) is "
              + ("stretched-high — little room before overbought." if rr >= 70
                 else "washed-out — room to bounce." if rr <= 30
                 else "mid-range — recovered, not thrusting."))
    cs = _cross_state(ml, sl)
    pl.append("MACD just turned up." if cs == "bullish"
              else "MACD just turned down." if cs == "bearish"
              else ("MACD still below its signal — not confirmed." if ml.iloc[-1] < sl.iloc[-1]
                    else "MACD above its signal — trend support intact."))
    if gate.available:
        pl.append(f"Intraday setup: {gate.note}.")

    return TechnicalsRead(
        as_of=as_of, spot=round(spot, 2), rsi14=round(float(rr), 1),
        macd_line=round(float(ml.iloc[-1]), 3), macd_signal=round(float(sl.iloc[-1]), 3),
        macd_hist=round(float(hl.iloc[-1]), 3), macd_cross=cs,
        volume=vr, shelves=shelves, gate=gate, plain_language=pl)
