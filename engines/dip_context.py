"""
dip_context.py — regime classify + conditional fwd-return stats + volume
forensics + plain language + confidence-gated verdict for "should I buy the
dip in X".

Division of labor (same contract as episodes.py / relative_strength.py):
  * Python computes every number. The LLM/UI only renders a `plain` field
    or a verdict pill — it never invents a probability or a confidence.
  * Confidence gates presentation, but it gates HOW a read is shown, not
    WHETHER it gets to assert a direction it never earned. See
    build_verdict() below — it reuses scripts/deflated_confidence.py's
    deflation formula, whose own label already says "low / likely mined"
    for exactly this case. A "low / likely mined" or too-thin-n read
    renders as INSUFFICIENT_EVIDENCE (we cannot judge this either way),
    never as WAIT (we have evidence and it says don't act) and never as a
    BUY/AVOID it can't support. Previously this module hard-forced WAIT on
    a mined read regardless of its point estimate's direction — which let
    a handful of regime-matched dips silently overrule a much larger
    ensemble's opposite-signed call elsewhere on the tear sheet, because
    WAIT reads as a real, opposing vote and INSUFFICIENT_EVIDENCE doesn't.
    Low confidence still changes the read, just not its direction: the
    stated range widens and shadow_size (how much weight this read should
    carry if ever sized into anything) shrinks toward zero.
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
  vix, oas   VIX close and credit-spread proxy (BAA10Y, not high-yield OAS
             -- see docs/CREDIT_SERIES.md) Series, own full history.
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
# Mirrors forecast_engine.regime_series / research_engine.credit_regime_
# series / vix_regime / spy_trend_regime — reimplemented locally, see
# module docstring for why. The credit dimension's percentile-window
# constants (CREDIT_PCTILE_*) and reasoning are owned by research_engine.
# credit_regime_series()'s docstring / docs/CREDIT_SERIES.md item 3 --
# kept in sync by hand here, same as everything else this function
# mirrors. Change one, change both.
CREDIT_PCTILE_WINDOW = 1260
CREDIT_PCTILE_LOW = 20
CREDIT_PCTILE_HIGH = 80


def _credit_regime_series(oas: pd.Series, idx):
    """Percentile-based credit label, mirrors research_engine.
    credit_regime_series() exactly (same window/cuts) -- see that
    function's docstring for the full reasoning (rolling not full-sample,
    percentile not absolute, BAA10Y not HY OAS)."""
    oas_al = oas.reindex(idx).ffill()
    chg = oas_al.diff(63)
    valid = chg.dropna()

    pct_rank = pd.Series(np.nan, index=chg.index)
    if len(valid) >= CREDIT_PCTILE_WINDOW:
        arr = valid.to_numpy()
        windows = np.lib.stride_tricks.sliding_window_view(arr, CREDIT_PCTILE_WINDOW)
        last = windows[:, -1]
        ranks = (windows < last[:, None]).mean(axis=1) * 100
        computed = pd.Series(np.nan, index=valid.index)
        computed.iloc[CREDIT_PCTILE_WINDOW - 1:] = ranks
        pct_rank.loc[computed.index] = computed

    return np.where(pct_rank.isna(), "unknown",
                     np.where(pct_rank >= CREDIT_PCTILE_HIGH, "widening",
                              np.where(pct_rank <= CREDIT_PCTILE_LOW, "narrowing", "flat")))


def _regime_series(idx, vix: pd.Series, oas: pd.Series, spy_close: pd.Series):
    vix_al = vix.reindex(idx).ffill()
    vix_lab = np.where(vix_al.isna(), "unknown",
                        np.where(vix_al < 20, "calm",
                                 np.where(vix_al <= 30, "elevated", "stressed")))

    credit_lab = _credit_regime_series(oas, idx)

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
    engine.regime_conditioned_positions (including its "unknown" must
    never match "unknown" guard -- see that function's comment and
    docs/CREDIT_SERIES.md) but pre-filtered to dip days."""
    for depth in (3, 2, 1, 0):
        if depth == 0:
            cand = list(dip_positions)
        elif "unknown" in current_tuple[:depth]:
            cand = []
        else:
            cand = [i for i in dip_positions
                    if regime_tuples[i][:depth] == current_tuple[:depth]
                    and "unknown" not in regime_tuples[i][:depth]]
        thinned = _thin_sequential(sorted(cand), gap)
        if len(thinned) >= min_n or depth == 0:
            return thinned, depth
    return [], 0


def _horizon_stats(close: pd.Series, spy_close_aligned: pd.Series, positions, horizon):
    n = len(close)
    rets, excess, mae, used_pos = [], [], [], []
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
        used_pos.append(pos)
        se, ee = spy_close_aligned.iloc[pos], spy_close_aligned.iloc[end]
        if pd.notna(se) and pd.notna(ee) and se > 0:
            excess.append(ret - float(ee / se - 1))
    if not rets:
        return None
    s = pd.Series(rets)
    # docs/CREDIT_SERIES.md #6.2: mirrors forecast_engine.horizon_stats()'s
    # date_start/date_end -- previously omitted here, so this voter's sample
    # provenance (which dates it's actually drawn from) was never recorded
    # anywhere, live or in the forecasts row. Entry dates of the positions
    # that survived the horizon's end<n filter, same definition as the
    # ensemble voter uses, so the two are comparable field-for-field.
    episode_dates = [close.index[p] for p in used_pos]
    date_start = min(episode_dates).date().isoformat() if episode_dates else None
    date_end = max(episode_dates).date().isoformat() if episode_dates else None
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
        "date_start": date_start,
        "date_end": date_end,
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
    """Confidence-gated BUY/WAIT/AVOID/INSUFFICIENT_EVIDENCE — see the
    module docstring for why INSUFFICIENT_EVIDENCE replaced the old
    hard-WAIT gate (BUILD.md's prime directive #3, corrected). This is the
    one place every caller gets the same rule from, same as before."""
    shadow_size = round(max(0.0, min(1.0, confidence_score / 100.0)), 2)

    if stats is None:
        return {
            "verdict": "INSUFFICIENT_EVIDENCE",
            "confidence_label": confidence_label or "low / likely mined",
            "confidence_score": confidence_score,
            "caveat": ("No independent historical episodes match today's dip "
                       "depth and regime closely enough to condition on — not "
                       "enough to judge this one either way."),
            "shadow_size": 0.0,
            "display_range": None,
        }

    q20, q80 = stats["q20"], stats["q80"]
    mined = confidence_label == "low / likely mined"
    if mined:
        # Deflated confidence says this read is statistically indistinguishable
        # from a mined pattern. That never flips or blocks a direction found
        # elsewhere — it only widens the stated range so the point estimate
        # isn't shown tighter than it deserves; shadow_size (above) already
        # shrinks with the same score.
        half_width = (q80 - q20) / 2
        q20, q80 = round(q20 - half_width, 4), round(q80 + half_width, 4)
    display_range = {"q20": q20, "q80": q80}

    if mined or stats["n"] < MIN_N:
        n = stats["n"]
        return {
            "verdict": "INSUFFICIENT_EVIDENCE",
            "confidence_label": confidence_label,
            "confidence_score": confidence_score,
            "caveat": (f"Only {n} comparable dip{'s' if n != 1 else ''} on "
                       "record — not enough to judge this one either way. "
                       "Not a call against acting; it simply can't outvote a "
                       "higher-sample read found elsewhere on the tear sheet."),
            "shadow_size": shadow_size,
            "display_range": display_range,
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
            "confidence_score": confidence_score, "caveat": None,
            "shadow_size": shadow_size, "display_range": display_range}


# ------------------------------------------------------------- entrypoint

def dip_context(df: pd.DataFrame, spy_close: pd.Series, vix: pd.Series,
                 oas: pd.Series, ticker: str, dip_threshold=DIP_THRESHOLD,
                 technical_gate_count: int = 0) -> dict:
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
    effective_depth = depth + technical_gate_count
    score, label = deflated_confidence(n, consistency, effective_depth, decades, searched)

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
        "technical_gate_count": technical_gate_count,
        "horizons": {str(h): horizon_results[h] for h in HORIZONS},
        "volume_forensics": vol,
        "verdict": verdict,
        "plain": plain,
    }
