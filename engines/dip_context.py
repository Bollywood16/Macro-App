"""
dip_context.py — regime classify + conditional fwd-return stats + volume
forensics + plain language + confidence-gated verdict for "should I buy the
dip in X".

Division of labor (same contract as episodes.py / relative_strength.py):
  * Python computes every number. The LLM/UI only renders a `plain` field
    or a verdict pill — it never invents a probability or a confidence.
  * Confidence gates presentation: a low-n or statistically-mined-looking
    read can NEVER surface as BUY. See build_verdict() below — it reuses
    scripts/deflated_confidence.py's deflation formula, whose own label
    already says "low / likely mined" for exactly this case, rather than
    inventing a second, possibly-inconsistent gating scheme.
  * This module is self-contained (no imports from scripts/) so it can be
    unit-tested and composed in isolation, matching how episodes.py and
    relative_strength.py already ship. The small pieces it needs from the
    scripts/ engines (regime thresholds, regime-conditioned position
    matching, horizon return stats) are re-expressed locally at the ~15-
    line size where a cross-directory import would cost more than it
    saves — see forecast_engine.py's regime_series/regime_conditioned_
    positions/horizon_stats for the originals this mirrors.

Input:
  df         OHLCV DataFrame for the ticker itself; needs 'close','volume'.
  spy_close  SPY close Series, own full history (excess-return benchmark).
  vix, oas   VIX close and FRED HY OAS Series, own full history.
  ticker     str, for plain-language sentences.
Output: JSON-serializable dict -> DipContextCard.
"""
from __future__ import annotations
import os
import sys
import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "scripts"))
try:
    from deflated_confidence import deflated_confidence
except ImportError:
    # scripts/ not importable from this working directory (e.g. ad hoc
    # invocation from elsewhere) — fall back to a local copy of the same
    # formula so this module still works standalone.
    import math

    def _z_score(n, consistency):
        if n <= 0:
            return 0.0
        se = math.sqrt(0.25 / n)
        return (consistency - 0.5) / se if se > 0 else 0.0

    def _expected_max_z(searched):
        return math.sqrt(2 * math.log(max(int(searched), 2)))

    def deflated_confidence(n, consistency, depth, decades, searched):
        z_obs = _z_score(n, consistency)
        if z_obs <= 0:
            deflate = 0.0
        else:
            z_chance = _expected_max_z(searched)
            excess = max(0.0, z_obs - z_chance)
            deflate = excess / (excess + z_chance) if (excess + z_chance) > 0 else 0.0
        base = (100 * min(1, n / 12) * consistency * (0.85 ** depth)
                * min(1, decades / 4))
        score = round(base * deflate)
        label = ("high" if score >= 70 else
                 "moderate" if score >= 40 else "low / likely mined")
        return score, label

DIP_THRESHOLD = -0.10     # a day counts as "in a dip" below this drawdown
EPISODE_GAP = 21          # independence spacing (trading days) for thinning
MIN_N = 8                 # regime-depth backoff floor, same as forecast_engine
HORIZONS = [21, 63]       # ~1mo, ~3mo dip-resolution windows
VOL_LOOKBACK = 10         # recent window for volume forensics


# ------------------------------------------------------- regime machinery
# Mirrors forecast_engine.regime_series / research_engine's vix_regime /
# credit_regime / spy_trend_regime — reimplemented locally, see module
# docstring for why.

def _regime_series(idx, vix: pd.Series, oas: pd.Series, spy_close: pd.Series):
    vix_al = vix.reindex(idx).ffill()
    vix_lab = np.where(vix_al.isna(), "unknown",
                        np.where(vix_al < 20, "calm",
                                 np.where(vix_al <= 30, "elevated", "stressed")))

    oas_al = oas.reindex(idx).ffill()
    chg = oas_al - oas_al.shift(63)
    credit_lab = np.where(chg.isna(), "unknown",
                           np.where(chg > 0.25, "widening",
                                    np.where(chg < -0.25, "narrowing", "flat")))

    spy_al = spy_close.reindex(idx).ffill()
    spy_ma200 = spy_al.rolling(200).mean()
    spy_lab = np.where(spy_ma200.isna(), "unknown",
                        np.where(spy_al >= spy_ma200, "above", "below"))

    return list(zip(vix_lab.tolist(), credit_lab.tolist(), spy_lab.tolist()))


def _thin_sequential(positions, gap):
    out, last = [], -10 ** 9
    for i in positions:
        if i - last >= gap:
            out.append(i)
            last = i
    return out


def _matched_positions(regime_tuples, dip_positions, current_tuple, gap, min_n):
    """Depth-backoff regime match (3-dim -> unconditional), restricted to
    dip days only, gap-thinned to independent episodes. Mirrors forecast_
    engine.regime_conditioned_positions but pre-filtered to dip days."""
    for depth in (3, 2, 1, 0):
        if depth == 0:
            cand = list(dip_positions)
        else:
            cand = [i for i in dip_positions
                    if regime_tuples[i][:depth] == current_tuple[:depth]]
        thinned = _thin_sequential(sorted(cand), gap)
        if len(thinned) >= min_n or depth == 0:
            return thinned, depth
    return [], 0


def _horizon_stats(close: pd.Series, spy_close_aligned: pd.Series, positions, horizon):
    n = len(close)
    rets, excess, mae = [], [], []
    for pos in positions:
        end = pos + horizon
        if end >= n:
            continue
        entry = float(close.iloc[pos])
        if entry <= 0:
            continue
        window = close.iloc[pos:end + 1] / entry - 1
        ret = float(window.iloc[-1])
        rets.append(ret)
        mae.append(float(window.min()))
        se, ee = spy_close_aligned.iloc[pos], spy_close_aligned.iloc[end]
        if pd.notna(se) and pd.notna(ee) and se > 0:
            excess.append(ret - float(ee / se - 1))
    if not rets:
        return None
    s = pd.Series(rets)
    return {
        "n": len(rets),
        "p_positive": round(float((s > 0).mean()), 4),
        "p_beat_benchmark": (round(float((pd.Series(excess) > 0).mean()), 4)
                              if excess else None),
        "q20": round(float(s.quantile(0.2)), 4),
        "q50": round(float(s.quantile(0.5)), 4),
        "q80": round(float(s.quantile(0.8)), 4),
        "expected_mae": round(float(np.mean(mae)), 4),
        "mean_excess_return": (round(float(np.mean(excess)), 4)
                                if excess else None),
    }


# ------------------------------------------------------------------ volume

def _volume_forensics(close: pd.Series, volume: pd.Series, lookback=VOL_LOOKBACK):
    if volume is None or volume.dropna().empty or len(volume) < 63:
        return {"available": False}
    down_day = close.diff() < 0
    recent_down_vol = volume[-lookback:][down_day[-lookback:]]
    trailing_avg_vol = volume.iloc[-63:-lookback].mean()
    if recent_down_vol.empty or not trailing_avg_vol or pd.isna(trailing_avg_vol):
        return {"available": False}
    ratio = float(recent_down_vol.mean() / trailing_avg_vol)
    if ratio >= 1.3:
        signature = "capitulation_like"
        note = ("Volume on down days over the last "
                f"{lookback} sessions is running ~{ratio:.1f}x the trailing "
                "average — consistent with capitulation-style selling, which "
                "historically marks bottoms more often than it extends them.")
    elif ratio <= 0.8:
        signature = "orderly_distribution"
        note = ("Volume on down days over the last "
                f"{lookback} sessions is only ~{ratio:.1f}x the trailing "
                "average — a quieter, orderly decline, which carries less "
                "of the exhaustion signature that typically precedes a low.")
    else:
        signature = "unclear"
        note = (f"Volume on down days is close to normal (~{ratio:.1f}x "
                 "trailing average) — no strong capitulation or distribution "
                 "signature either way.")
    return {"available": True, "down_day_vol_ratio": round(ratio, 2),
            "signature": signature, "note": note}


# ---------------------------------------------------------------- verdict

def build_verdict(stats, confidence_score, confidence_label):
    """Confidence-gated, equal-weight BUY/WAIT/AVOID. A low/likely-mined
    read is capped at WAIT regardless of which way the edge points — this
    is the rule referenced by BUILD.md's prime directive #3, kept in one
    place so every caller gets the same gate."""
    if stats is None or confidence_label == "low / likely mined":
        return {
            "verdict": "WAIT",
            "confidence_label": confidence_label or "low / likely mined",
            "confidence_score": confidence_score,
            "caveat": ("Sample is too small, or statistically indistinguishable "
                       "from a mined pattern, to support a call either way. "
                       "Amber-capped regardless of apparent direction."),
        }
    edge = stats["p_positive"] - 0.5
    downside = stats["expected_mae"]
    if edge <= -0.08 or (downside is not None and downside < -0.12):
        verdict = "AVOID"
    elif edge >= 0.08:
        verdict = "BUY"
    else:
        verdict = "WAIT"
    return {"verdict": verdict, "confidence_label": confidence_label,
            "confidence_score": confidence_score, "caveat": None}


# ------------------------------------------------------------- entrypoint

def dip_context(df: pd.DataFrame, spy_close: pd.Series, vix: pd.Series,
                 oas: pd.Series, ticker: str, dip_threshold=DIP_THRESHOLD) -> dict:
    close = df["close"]
    volume = df["volume"] if "volume" in df.columns else None
    idx = close.index

    roll_high = close.rolling(252, min_periods=60).max()
    dd = close / roll_high - 1
    cur_dd = float(dd.iloc[-1]) if pd.notna(dd.iloc[-1]) else None

    regimes = _regime_series(idx, vix, oas, spy_close)
    current_tuple = regimes[-1]

    dip_positions = np.flatnonzero((dd <= dip_threshold).values)
    positions, depth = _matched_positions(regimes, dip_positions, current_tuple,
                                           EPISODE_GAP, MIN_N)

    spy_al = spy_close.reindex(idx).ffill()
    horizon_results = {}
    for h in HORIZONS:
        horizon_results[h] = _horizon_stats(close, spy_al, positions, h)

    primary = horizon_results.get(HORIZONS[0])
    n = len(positions)
    decades = len({idx[p].year // 10 for p in positions}) if positions else 0
    searched = max(1, 4 - depth)  # how many depth levels were tried before landing
    consistency = max(primary["p_positive"], 1 - primary["p_positive"]) if primary else 0.0
    score, label = deflated_confidence(n, consistency, depth, decades, searched)

    verdict = build_verdict(primary, score, label)
    vol = _volume_forensics(close, volume)

    plain = []
    if cur_dd is not None:
        plain.append(f"{ticker} is {abs(cur_dd) * 100:.1f}% below its trailing 52-week high.")
    plain.append(
        f"Today's regime: VIX {current_tuple[0]}, credit {current_tuple[1]}, "
        f"SPY trend {current_tuple[2]}.")
    if primary and primary["n"]:
        plain.append(
            f"In {primary['n']} past episodes with a comparable dip depth and regime, "
            f"{primary['p_positive'] * 100:.0f}% were higher {HORIZONS[0]} trading days later "
            f"(median {primary['q50'] * 100:+.1f}%).")
    else:
        plain.append("No independent historical episodes match today's dip depth and regime closely enough to condition on.")
    if vol.get("available"):
        plain.append(vol["note"])
    if verdict["caveat"]:
        plain.append(verdict["caveat"])

    return {
        "ticker": ticker,
        "current_drawdown_pct": round(cur_dd * 100, 1) if cur_dd is not None else None,
        "regime": {"vix": current_tuple[0], "credit": current_tuple[1], "spy_trend": current_tuple[2]},
        "regime_match_depth": depth,
        "horizons": {str(h): horizon_results[h] for h in HORIZONS},
        "volume_forensics": vol,
        "verdict": verdict,
        "plain": plain,
    }
