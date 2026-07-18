"""
relative_strength.py — rolling relative-performance extremes vs. multiple
benchmarks, with fat-tail-aware flagging and historical resolution.

Contract (same as the other engines):
  * Python computes; UI/LLM only interpret.
  * Report percentile FIRST, z-score second — return distributions are
    fat-tailed, so a naive sigma overstates rarity. Percentile-vs-own-history
    is the honest extremity measure; z is a familiar secondary tag.
  * Every extreme ships with how comparable past extremes RESOLVED, plus a
    confidence score gated on sample size.

Input: dict of aligned close-price Series {ticker: Series, benchmarks...}.
Output: JSON-serializable dict -> RelativeStrengthCard.
"""
from __future__ import annotations
import numpy as np
import pandas as pd

WINDOWS = [20, 50, 100, 200]         # rolling lookbacks (trading days)
RESOLVE_HORIZON = 40                 # ~8 weeks: how we measure "reverted"
EXTREME_PCTILE = 90                  # |pctile-50|*2 >= this => flag
EPISODE_SPACING = 40                 # independence spacing for resolution stats


def _log_rel(a: pd.Series, b: pd.Series) -> pd.Series:
    """Cumulative relative performance of a vs b (log), aligned."""
    j = pd.concat([a, b], axis=1).dropna()
    return np.log(j.iloc[:, 0]) - np.log(j.iloc[:, 1])


def _rolling_rel_return(rel: pd.Series, window: int) -> pd.Series:
    """Relative return of a-vs-b over the trailing `window` days."""
    return rel - rel.shift(window)


def _pctile_and_z(series: pd.Series):
    """Current reading's percentile within its own history, plus z-score."""
    s = series.dropna()
    if len(s) < 60:
        return None, None
    cur = s.iloc[-1]
    pct = (s < cur).mean() * 100
    z = (cur - s.mean()) / (s.std(ddof=1) + 1e-9)
    return round(float(pct), 1), round(float(z), 2)


def _resolution(rel_ret: pd.Series, window: int):
    """How comparable past extremes resolved: of the independent historical
    days where rolling rel-return was as high as today, how often did it fall
    over the next RESOLVE_HORIZON days, and by how much (median)."""
    s = rel_ret.dropna()
    if len(s) < window + RESOLVE_HORIZON + 60:
        return {"n": 0}
    cur = s.iloc[-1]
    thresh = np.quantile(s.iloc[:-1], 0.90)
    if cur < thresh:                                   # not currently extreme
        return {"n": 0, "not_extreme": True}
    hits = np.flatnonzero((s.values >= thresh))
    episodes, last = [], -EPISODE_SPACING
    for pos in hits:
        if pos - last < EPISODE_SPACING:
            continue
        if pos + RESOLVE_HORIZON >= len(s):
            continue
        fwd = s.iloc[pos + RESOLVE_HORIZON] - s.iloc[pos]   # change in rel perf
        episodes.append(fwd)
        last = pos
    if not episodes:
        return {"n": 0}
    arr = np.array(episodes)
    return {
        "n": len(arr),
        "pct_reverted": round(float((arr < 0).mean() * 100), 0),
        "median_giveback_pct": round(float(np.median(arr) * 100), 1),
    }


def _label(pct):
    if pct is None:
        return "insufficient_history"
    ext = abs(pct - 50) * 2
    if pct >= 50:
        return "hot" if ext >= EXTREME_PCTILE else ("warm" if ext >= 60 else "normal")
    return "cooling" if ext >= 60 else "normal"


def relative_strength(prices: dict[str, pd.Series], ticker: str,
                      benchmarks: dict[str, str]) -> dict:
    """
    prices: {symbol: close Series}; must include `ticker` and each benchmark symbol.
    benchmarks: {label: symbol}, e.g. {"S&P 500":"SPY","Nasdaq 100":"QQQ","Peers":"SOXX"}
    """
    base = prices[ticker]
    rows, flagged = [], None
    for label, sym in benchmarks.items():
        if sym not in prices:
            continue
        rel = _log_rel(base, prices[sym])
        for w in WINDOWS:
            rr = _rolling_rel_return(rel, w)
            pct, z = _pctile_and_z(rr)
            lab = _label(pct)
            row = {"vs": label, "symbol": sym, "window": w,
                   "pctile": pct, "z": z, "state": lab}
            if lab == "hot":
                res = _resolution(rr, w)
                row["resolution"] = res
                # headline the most extreme hot reading
                if flagged is None or (pct or 0) > (flagged.get("pctile") or 0):
                    flagged = row
            rows.append(row)

    # confidence for the flagged extreme: sample size of its resolution set
    conf = {"score": 0, "label": "no data"}
    if flagged and flagged.get("resolution", {}).get("n"):
        n = flagged["resolution"]["n"]
        score = min(100, round(100 * min(1.0, n / 12) *
                    (0.5 + 0.5 * abs((flagged["resolution"]["pct_reverted"] - 50) / 50))))
        conf = {"score": score,
                "label": "high" if score >= 70 else "moderate" if score >= 45 else "low"}

    plain = []
    if flagged:
        r = flagged.get("resolution", {})
        plain.append(
            f"{ticker} has outrun {flagged['vs']} over {flagged['window']} days more than "
            f"~{flagged['pctile']:.0f}% of its own history — a stretched reading.")
        if r.get("n"):
            plain.append(
                f"The last {r['n']} times it got this stretched, it narrowed within ~8 weeks "
                f"{r['pct_reverted']:.0f}% of the time (median give-back {r['median_giveback_pct']:.1f}%).")
    else:
        plain.append(f"{ticker} is not at a relative-performance extreme versus its benchmarks right now.")

    return {
        "ticker": ticker, "flagged": flagged, "confidence": conf,
        "rows": rows, "plain": plain,
    }
