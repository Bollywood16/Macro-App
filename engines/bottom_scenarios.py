"""
bottom_scenarios.py — Monte Carlo trough estimation for "if this is a dip,
where and when does it likely find a low."

Two branches per simulated path, blended by weight:
  * episode-sampled  -- resample actual forward-return paths from this
    ticker's own past dips at a comparable drawdown depth. The "history
    rhymes" branch.
  * break branch     -- block-bootstrap of the ticker's UNCONDITIONAL daily
    return history (not dip-conditioned), representing "this time has no
    precedent" tail risk. Its weight rises automatically as episode
    history thins out, so a data-poor ticker doesn't inherit false
    confidence from a handful of episodes.

Division of labor, same as the other modules in this directory: Python
computes every quantile; the UI only renders the three stat tiles and the
fan chart this module returns.

Input:
  df              OHLCV DataFrame; needs 'close'.
  ticker          str, for plain-language sentences.
  dip_threshold   drawdown-from-252d-high that counts as "in a dip" —
                   scenarios only run against an active dip.
Output: JSON-serializable dict -> Monte Carlo stat tiles + fan chart.
"""
from __future__ import annotations
from datetime import datetime, timezone
import numpy as np
import pandas as pd

DIP_THRESHOLD = -0.10
EPISODE_GAP = 21
DEPTH_TOL = 0.06            # how close a past drawdown must be to "comparable"
HORIZON = 63                # ~3mo simulated forward window (trading days)
N_SIMS = 2000
BLOCK = 5                   # break-branch bootstrap block size (trading days)
MIN_EPISODES_FULL_WEIGHT = 10
MAX_HIST_WEIGHT = 0.7       # even with abundant episodes, keep some break-branch weight
# REFINEMENT.md §2: the break-branch weight is the highest-value lever in
# this module and currently invisible/fixed. Rather than re-run the whole
# simulation server-side on every slider drag, two independent PURE pools
# (100% history-branch, 100% break-branch) are generated once here; the UI
# recombines them into a weighted mixture client-side for any weight in
# SLIDER_RANGE, which is exact (mixture-CDF quantiles), not an approximation
# — Python still does every draw, the UI only re-weights what's already
# been simulated.
SLIDER_POOL_SIZE = 600
SLIDER_MIN_W_BREAK = 0.05
SLIDER_MAX_W_BREAK = 0.50


def _episode_paths(close: pd.Series, dd: pd.Series, cur_dd: float, horizon: int,
                    tol: float, gap: int):
    n = len(close)
    candidates = np.flatnonzero(((dd - cur_dd).abs() <= tol).values)
    positions, last = [], -10 ** 9
    for pos in sorted(candidates):
        if pos - last < gap:
            continue
        if pos + horizon >= n:
            continue
        last = pos
        positions.append(pos)
    paths, dates = [], []
    for pos in positions:
        entry = float(close.iloc[pos])
        if entry <= 0:
            continue
        window = close.iloc[pos:pos + horizon + 1] / entry - 1
        paths.append(window.to_numpy()[1:])  # cumulative return, days 1..horizon
        dates.append(close.index[pos])
    return paths, dates


def _break_branch_paths(daily_rets: np.ndarray, horizon: int, n_sims: int,
                         block: int, rng: np.random.Generator):
    n = len(daily_rets)
    block = min(block, max(1, n - 1))
    n_blocks = int(np.ceil(horizon / block))
    paths = np.empty((n_sims, horizon))
    for i in range(n_sims):
        chunks = [daily_rets[start:start + block]
                  for start in rng.integers(0, max(1, n - block), size=n_blocks)]
        seq = np.concatenate(chunks)[:horizon]
        paths[i] = np.cumprod(1 + seq) - 1
    return paths


def _pool_trough_returns(paths: list, n: int, rng: np.random.Generator):
    """n trough returns (worst point reached) bootstrap-resampled purely
    from `paths` (a list of return-path arrays) — used for the slider's
    100%-history pool. Empty if there are no episodes to draw from."""
    if not paths:
        return np.array([])
    idx = rng.integers(0, len(paths), size=n)
    sims = np.array([paths[i] for i in idx])
    return np.minimum.accumulate(sims, axis=1)[:, -1]


def bottom_scenarios(df: pd.DataFrame, ticker: str, dip_threshold=DIP_THRESHOLD,
                      horizon=HORIZON, n_sims=N_SIMS) -> dict:
    close = df["close"]
    if len(close) < 120:
        return {"ticker": ticker, "available": False,
                "plain": [f"Not enough history for {ticker} to run trough scenarios."]}

    roll_high = close.rolling(252, min_periods=60).max()
    dd = close / roll_high - 1
    cur_dd = float(dd.iloc[-1]) if pd.notna(dd.iloc[-1]) else None

    if cur_dd is None or cur_dd > dip_threshold:
        return {
            "ticker": ticker, "available": False,
            "plain": [f"{ticker} is not currently in a dip "
                      f"(threshold {dip_threshold * 100:.0f}%) — Monte Carlo "
                      "trough scenarios only run against an active dip."],
        }

    episode_paths, episode_dates = _episode_paths(close, dd, cur_dd, horizon,
                                                   DEPTH_TOL, EPISODE_GAP)
    n_episodes = len(episode_paths)

    rng = np.random.default_rng()
    daily_rets = close.pct_change().dropna().to_numpy()

    w_hist = min(MAX_HIST_WEIGHT, MAX_HIST_WEIGHT * n_episodes / MIN_EPISODES_FULL_WEIGHT)
    n_hist = int(round(n_sims * w_hist)) if n_episodes else 0
    n_break = n_sims - n_hist

    sims = np.empty((n_sims, horizon))
    if n_hist:
        idx = rng.integers(0, n_episodes, size=n_hist)
        for i, ep in enumerate(idx):
            sims[i] = episode_paths[ep]
    if n_break:
        sims[n_hist:] = _break_branch_paths(daily_rets, horizon, n_break, BLOCK, rng)

    running_min = np.minimum.accumulate(sims, axis=1)
    trough_ret = running_min[:, -1]  # worst point reached over the horizon, per sim

    # REFINEMENT.md §2: pure-branch pools for the client-side slider — see
    # SLIDER_POOL_SIZE comment above. Independent of the blended `sims`
    # above (different rng draws), which is fine: both are unbiased samples
    # of their respective branch, and the default tiles below still come
    # from the engine's own blended `sims`, not from recombining these.
    slider_hist_pool = _pool_trough_returns(episode_paths, SLIDER_POOL_SIZE, rng)
    slider_break_pool = _break_branch_paths(daily_rets, horizon, SLIDER_POOL_SIZE, BLOCK, rng)
    slider_break_pool = np.minimum.accumulate(slider_break_pool, axis=1)[:, -1]

    pct_low_in = round(float((trough_ret >= -0.005).mean()) * 100, 0)
    likely_trough_pct = round(float(np.median(trough_ret)) * 100, 1)
    tail_risk_pct = round(float(np.quantile(trough_ret, 0.10)) * 100, 1)

    # REFINEMENT.md §6: name the staging levels directly instead of leaving
    # the user to turn a trough PERCENTAGE back into a price and a tranche
    # decision by hand. Tranche 1 is the current price (already-deployed
    # entry); tranche 2/3 are where the 25th/10th-percentile trough would
    # actually land in dollars.
    current_price = float(close.iloc[-1])
    p25_trough_pct = round(float(np.quantile(trough_ret, 0.25)) * 100, 1)
    p25_price = round(current_price * (1 + p25_trough_pct / 100), 2)
    p10_price = round(current_price * (1 + tail_risk_pct / 100), 2)
    ladder = [
        {"tranche": 1, "label": "Tranche 1 (current)", "pctile": None,
         "return_pct": 0.0, "price": round(current_price, 2)},
        {"tranche": 2, "label": "Tranche 2 (25th-pctile trough)", "pctile": 25,
         "return_pct": p25_trough_pct, "price": p25_price},
        {"tranche": 3, "label": "Tranche 3 (10th-pctile / tail-risk trough)", "pctile": 10,
         "return_pct": tail_risk_pct, "price": p10_price},
    ]
    ladder_plain = (
        f"25th-percentile trough ≈ ${p25_price:,.0f} → tranche 2 · "
        f"10th-percentile ≈ ${p10_price:,.0f} → tranche 3."
    )

    pctiles = [10, 25, 50, 75, 90]
    fan = {
        "days": list(range(1, horizon + 1)),
        **{f"p{p}": [round(float(np.quantile(sims[:, d], p / 100)) * 100, 2)
                     for d in range(horizon)]
           for p in pctiles},
    }

    score = round(100 * min(1, n_episodes / MIN_EPISODES_FULL_WEIGHT))
    label = ("high" if score >= 70 else
              "moderate" if score >= 40 else "low / thin precedent")

    plain = [
        (f"{n_episodes} past episode(s) at a comparable drawdown depth found in "
         f"{ticker}'s own history, weighted {w_hist * 100:.0f}% of the simulation; "
         f"the rest draws from {ticker}'s unconditional daily-return distribution "
         "as a no-precedent check."
         if n_episodes else
         f"No comparable past episode found in {ticker}'s history — this simulation "
         "runs entirely on its unconditional daily-return distribution."),
        f"~{pct_low_in:.0f}% of simulated paths never meaningfully undercut today's "
        f"price over the next {horizon} trading days.",
        f"Median simulated trough (if one hasn't already formed): "
        f"{likely_trough_pct:+.1f}% from today.",
        f"Tail-risk scenario (10th percentile): {tail_risk_pct:+.1f}% from today.",
        ladder_plain,
    ]
    if n_episodes < MIN_EPISODES_FULL_WEIGHT:
        plain.append("Thin precedent — treat these bands as wide-uncertainty estimates, "
                      "not tight forecasts.")

    # REFINEMENT.md §1: the tiles alone don't say these are TROUGH stats
    # (the worst point reached along a path), not endpoint stats — that's
    # exactly what collides with the forecast strip's "X% higher" framing
    # unless it's said plainly, once, right under the tiles. Deterministic
    # template, not LLM prose, per this file's own division-of-labor rule.
    explainer = (
        f"Most simulated paths bottom around {likely_trough_pct:+.1f}% from today before "
        f"recovering; about {pct_low_in:.0f}% of paths suggest the low may already be in. "
        f"A worse 1-in-10 case falls to {tail_risk_pct:+.1f}% from here. These numbers "
        "describe the lowest point reached along the way, not where the price ends up — "
        "the forecast strip below covers endpoints, not troughs."
    )

    w_break = round(1 - w_hist, 4)
    w_hist_reason = (
        f"{n_episodes} comparable past episode(s) found in {ticker}'s own history "
        f"(full weight needs {MIN_EPISODES_FULL_WEIGHT}+)" if n_episodes
        else f"no comparable past episode found in {ticker}'s history"
    )
    assumptions = {
        "n_sims": n_sims,
        "n_episodes": n_episodes,
        "episode_dates": [d.date().isoformat() for d in episode_dates],
        "sample_date_start": (min(episode_dates).date().isoformat()
                               if episode_dates else None),
        "sample_date_end": (max(episode_dates).date().isoformat()
                             if episode_dates else None),
        # "break" = unconditional bootstrap (thesis-break / no-precedent branch),
        # "history" = episode-sampled (positioning-flush / "this rhymes" branch).
        "w_history": round(w_hist, 4), "w_break": w_break,
        "w_history_reason": w_hist_reason,
        "depth_tolerance_pct": round(DEPTH_TOL * 100, 1),
        "horizon_days": horizon,
        "block_size_days": BLOCK,
        "params_fixed": True,  # depth tolerance / horizon / block are fixed
                                # constants, not estimated per-ticker — say so
                                # honestly rather than imply otherwise.
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "slider": {
            "min_w_break": SLIDER_MIN_W_BREAK, "max_w_break": SLIDER_MAX_W_BREAK,
            "default_w_break": w_break,
            "history_pool_troughs": [round(float(v), 4) for v in slider_hist_pool],
            "break_pool_troughs": [round(float(v), 4) for v in slider_break_pool],
        },
    }

    return {
        "ticker": ticker, "available": True,
        "current_drawdown_pct": round(cur_dd * 100, 1),
        "n_episodes": n_episodes, "horizon_days": horizon,
        "stats": {"pct_low_in": pct_low_in, "likely_trough_pct": likely_trough_pct,
                  "tail_risk_pct": tail_risk_pct},
        "confidence": {"score": score, "label": label},
        "fan_chart": fan,
        "plain": plain,
        "explainer": explainer,
        "ladder": ladder, "ladder_plain": ladder_plain,
        "assumptions": assumptions,
    }


def to_ballot(result: dict) -> dict:
    """ONE voter for the vote-aggregation engine (agreement_engine, module
    D). NOT a verdict. Ships calibrated=False / weight 0 until scored
    against matured outcomes, same as every other secondary voter."""
    stats = result.get("stats") or {}
    downside_pct = max(0.0, -(stats.get("likely_trough_pct") or 0.0))
    if not result.get("available"):
        vote = "WAIT"          # no active dip to simulate against -- no read
    elif downside_pct > 10:
        vote = "AVOID"
    elif downside_pct < 3:
        vote = "BUY"
    else:
        vote = "WAIT"          # covers 5-10% explicitly, and the unstated 3-5% band by continuity
    return {"voter": "bottom_scenarios", "vote": vote, "raw_confidence": 0.55,
            "calibrated": False, "weight_until_calibrated": 0.0,
            "independent_n": result.get("n_episodes", 0),
            "rationale": "; ".join(result.get("plain", [])[:2])}
