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
    paths = []
    for pos in positions:
        entry = float(close.iloc[pos])
        if entry <= 0:
            continue
        window = close.iloc[pos:pos + horizon + 1] / entry - 1
        paths.append(window.to_numpy()[1:])  # cumulative return, days 1..horizon
    return paths


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

    episode_paths = _episode_paths(close, dd, cur_dd, horizon, DEPTH_TOL, EPISODE_GAP)
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

    pct_low_in = round(float((trough_ret >= -0.005).mean()) * 100, 0)
    likely_trough_pct = round(float(np.median(trough_ret)) * 100, 1)
    tail_risk_pct = round(float(np.quantile(trough_ret, 0.10)) * 100, 1)

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
    ]
    if n_episodes < MIN_EPISODES_FULL_WEIGHT:
        plain.append("Thin precedent — treat these bands as wide-uncertainty estimates, "
                      "not tight forecasts.")

    return {
        "ticker": ticker, "available": True,
        "current_drawdown_pct": round(cur_dd * 100, 1),
        "n_episodes": n_episodes, "horizon_days": horizon,
        "stats": {"pct_low_in": pct_low_in, "likely_trough_pct": likely_trough_pct,
                  "tail_risk_pct": tail_risk_pct},
        "confidence": {"score": score, "label": label},
        "fan_chart": fan,
        "plain": plain,
    }
