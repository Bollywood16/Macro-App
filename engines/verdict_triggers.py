"""
verdict_triggers.py — ROBUSTNESS_FINAL module C: verdict-change triggers
with live met/not-met status (the "armed alarm"). WAIT is not a dead end —
this computes, for every ticker, exactly what would need to happen for the
read to upgrade toward BUY/STARTER or downgrade toward AVOID/REDUCE.

Division of labor (same contract as every other module in this directory):
  * Python computes every trigger's current value and met/not-met state.
    The UI only renders the checklist; it never decides anything.
  * DISPLAY / RECORD ONLY. Nothing in this module writes a verdict, and
    nothing elsewhere reads a trigger's `met` flag to gate or alter a
    verdict — the threshold-crossing convergence_alert flag from
    ROBUSTNESS_FINAL §4 requirement 4 is explicitly deferred, not built
    here.
  * Self-contained: recomputes the small amount of relative-return math it
    needs locally (mirrors dip_context.py's own local regime helpers)
    rather than depending on relative_strength.py's return shape, so that
    module's existing contract stays untouched.
  * "Crosses above/falls below X" is read as a snapshot comparison (is the
    current value on the triggered side right now), not an edge-transition
    detector — consistent with how every other engine in this app reports
    state (dip_context's regime, tech_read's trend).

Input:
  ohlcv                     OHLCV DataFrame for the ticker; needs 'close',
                             'low', 'volume'.
  spy_close                 SPY close Series, own full history (the app's
                             single canonical benchmark — BENCHMARK="SPY"
                             in forecast_engine.py).
  bottom_scenarios_result   dict returned by bottom_scenarios.bottom_scenarios()
                             for the same ticker/run — supplies the Monte
                             Carlo p25 trough estimate.
Output: list[dict], each {id, direction, metric, operator, threshold,
  current, met, plain} — JSON-serializable, no dataclasses.
"""
from __future__ import annotations
import numpy as np
import pandas as pd

MA20, MA200 = 20, 200
CONSEC_ABOVE_MA20 = 3
RS_SHORT_WINDOW = 10
RS_SHORT_QUIET_SESSIONS = 5
RS_LONG_WINDOW = 100
RS_LONG_LOOKBACK = 126        # ~6 months of trading days
UPDOWN_VOL_WINDOW = 10
UPDOWN_VOL_UPGRADE_THRESH = 1.2
UPDOWN_VOL_DOWNGRADE_THRESH = 0.6
UPDOWN_VOL_FALL_LOOKBACK = 5
REALIZED_VOL_WINDOW = 20
REALIZED_VOL_LOOKBACK = 10
NEW_LOW_WINDOW = 20            # matches the other short-horizon triggers here
SWING_FRACTAL = 5              # bars each side required to confirm a swing low
SWING_SEARCH_WINDOW = 60


def _log_rel(a: pd.Series, b: pd.Series) -> pd.Series:
    j = pd.concat([a, b], axis=1).dropna()
    return np.log(j.iloc[:, 0]) - np.log(j.iloc[:, 1])


def _rolling_rel_return(rel: pd.Series, window: int) -> pd.Series:
    return rel - rel.shift(window)


def _swing_lows(low: pd.Series, fractal: int = SWING_FRACTAL,
                 search_window: int = SWING_SEARCH_WINDOW):
    """Most recent two confirmed fractal swing lows (a day's low is the min
    of the `fractal` days on either side of it), searched over the trailing
    `search_window` sessions. Returns (prior, latest) prices, (None, None)
    if fewer than two are found."""
    w = low.tail(search_window)
    n = len(w)
    confirmed = []
    for i in range(fractal, n - fractal):
        window = w.iloc[i - fractal:i + fractal + 1]
        if w.iloc[i] == window.min():
            confirmed.append(round(float(w.iloc[i]), 2))
    if len(confirmed) < 2:
        return None, None
    return confirmed[-2], confirmed[-1]


def _consecutive_above(close: pd.Series, ma: pd.Series) -> int:
    above = (close >= ma).to_numpy()
    count = 0
    for v in above[::-1]:
        if not v:
            break
        count += 1
    return count


def _up_down_vol_ratio(close: pd.Series, volume: pd.Series, window: int):
    w_close, w_vol = close.tail(window + 1), volume.tail(window + 1)
    diffs = w_close.diff().dropna()
    vols = w_vol.iloc[1:]
    up_vol = vols[diffs >= 0].sum()
    dn_vol = vols[diffs < 0].sum()
    if dn_vol <= 0:
        return None
    return round(float(up_vol / dn_vol), 2)


def _new_low(close: pd.Series, window: int) -> bool:
    w = close.tail(window)
    return bool(w.iloc[-1] <= w.min())


def compute_triggers(ohlcv: pd.DataFrame, spy_close: pd.Series,
                      bottom_scenarios_result: dict | None) -> list:
    close, low, volume = ohlcv["close"], ohlcv["low"], ohlcv["volume"]
    spy_aligned = spy_close.reindex(close.index).ffill()
    triggers = []

    # ---------------------------------------------------------- upgrade 1
    ma20 = close.rolling(MA20).mean()
    consec = _consecutive_above(close, ma20)
    triggers.append({
        "id": "above_ma20_3d", "direction": "upgrade",
        "metric": "consecutive sessions closing above 20-day average",
        "operator": ">=", "threshold": CONSEC_ABOVE_MA20, "current": consec,
        "met": consec >= CONSEC_ABOVE_MA20,
        "plain": f"Closed above its 20-day average for {consec} straight "
                 f"session(s) (needs {CONSEC_ABOVE_MA20}).",
    })

    # ------------------------------------------------- upgrade 2 (swing low)
    prior_low, latest_low = _swing_lows(low)
    higher_low = (prior_low is not None and latest_low is not None
                  and latest_low > prior_low)
    triggers.append({
        "id": "higher_swing_low", "direction": "upgrade",
        "metric": "latest confirmed swing low vs. prior swing low",
        "operator": ">", "threshold": prior_low, "current": latest_low,
        "met": bool(higher_low),
        "plain": (f"Latest swing low (${latest_low:.2f}) is higher than the "
                  f"prior one (${prior_low:.2f})."
                  if prior_low is not None and latest_low is not None else
                  "Not enough confirmed swing lows in the trailing window yet."),
    })

    # -------------------------------------- upgrade 3 (short-horizon RS)
    rel = _log_rel(close, spy_aligned)
    rr_short = _rolling_rel_return(rel, RS_SHORT_WINDOW).dropna()
    quiet_sessions = 0
    if len(rr_short) > RS_SHORT_WINDOW:
        running_min = rr_short.expanding().min()
        is_new_low = (rr_short <= running_min).to_numpy()
        idx_true = np.flatnonzero(is_new_low)
        quiet_sessions = int(len(rr_short) - 1 - idx_true[-1]) if len(idx_true) else len(rr_short)
    triggers.append({
        "id": "rs_short_stops_new_lows", "direction": "upgrade",
        "metric": f"sessions since {RS_SHORT_WINDOW}d relative-return (vs SPY) last made a new low",
        "operator": ">=", "threshold": RS_SHORT_QUIET_SESSIONS, "current": quiet_sessions,
        "met": quiet_sessions >= RS_SHORT_QUIET_SESSIONS,
        "plain": f"{RS_SHORT_WINDOW}-day relative strength vs SPY hasn't made a new low in "
                 f"{quiet_sessions} session(s) (needs {RS_SHORT_QUIET_SESSIONS}).",
    })

    # ----------------------------- upgrade 4 / downgrade 3 (up/down vol ratio)
    ratio_now = _up_down_vol_ratio(close, volume, UPDOWN_VOL_WINDOW)
    if len(close) > UPDOWN_VOL_FALL_LOOKBACK:
        close_5d_ago = close.iloc[:-UPDOWN_VOL_FALL_LOOKBACK]
        vol_5d_ago = volume.iloc[:-UPDOWN_VOL_FALL_LOOKBACK]
        ratio_5d_ago = _up_down_vol_ratio(close_5d_ago, vol_5d_ago, UPDOWN_VOL_WINDOW)
    else:
        ratio_5d_ago = None
    triggers.append({
        "id": "updown_vol_ratio_above_1.2", "direction": "upgrade",
        "metric": "10-day up-volume / down-volume ratio",
        "operator": ">=", "threshold": UPDOWN_VOL_UPGRADE_THRESH, "current": ratio_now,
        "met": ratio_now is not None and ratio_now >= UPDOWN_VOL_UPGRADE_THRESH,
        "plain": (f"10-day up/down volume ratio is {ratio_now:.2f}."
                  if ratio_now is not None else
                  "Not enough volume data for a 10-day up/down ratio."),
    })

    # ---------------------------------------------------------- upgrade 5
    daily_ret = close.pct_change()
    rv = daily_ret.rolling(REALIZED_VOL_WINDOW).std()
    rv_now = round(float(rv.iloc[-1]), 4) if pd.notna(rv.iloc[-1]) else None
    rv_prior = (round(float(rv.iloc[-1 - REALIZED_VOL_LOOKBACK]), 4)
                if len(rv) > REALIZED_VOL_LOOKBACK and pd.notna(rv.iloc[-1 - REALIZED_VOL_LOOKBACK])
                else None)
    holds_low = latest_low is not None and float(close.iloc[-1]) >= latest_low
    vol_falling = rv_now is not None and rv_prior is not None and rv_now < rv_prior
    triggers.append({
        "id": "vol20d_falling_above_low", "direction": "upgrade",
        "metric": "20-day realized volatility now vs. 10 sessions ago, with price "
                  "holding the latest swing low",
        "operator": "<", "threshold": rv_prior, "current": rv_now,
        "met": bool(vol_falling and holds_low),
        "plain": ("Realized volatility is easing and price is holding above its "
                  "latest swing low." if vol_falling and holds_low else
                  "Realized volatility hasn't eased and/or price hasn't held the "
                  "latest swing low."),
    })

    # ---------------------------------------------------------- downgrade 1
    ticker_new_low = _new_low(close, NEW_LOW_WINDOW)
    spy_new_low = _new_low(spy_aligned, NEW_LOW_WINDOW)
    fires = ticker_new_low and not spy_new_low
    triggers.append({
        "id": "new_low_benchmark_holds", "direction": "downgrade",
        "metric": f"new {NEW_LOW_WINDOW}-day closing low while SPY holds its {NEW_LOW_WINDOW}-day low",
        "operator": "==", "threshold": True, "current": bool(fires),
        "met": bool(fires),
        "plain": ("Made a new closing low while SPY held its own — a relative "
                  "breakdown, not a market-wide move." if fires else
                  "No divergent new-low breakdown vs. SPY right now."),
    })

    # ---------------------------------------------------------- downgrade 2
    ma200 = close.rolling(MA200).mean()
    slope = None
    if len(ma200) > 20 and pd.notna(ma200.iloc[-1]) and pd.notna(ma200.iloc[-21]):
        slope = round(float(ma200.iloc[-1] - ma200.iloc[-21]), 2)
    triggers.append({
        "id": "ma200_slope_negative", "direction": "downgrade",
        "metric": "200-day average, now vs. 20 sessions ago",
        "operator": "<", "threshold": 0.0, "current": slope,
        "met": slope is not None and slope < 0,
        "plain": (f"200-day average has turned down over the last 20 sessions ({slope:+.2f})."
                  if slope is not None and slope < 0 else
                  "200-day average isn't sloping down." if slope is not None else
                  "Not enough history yet for a 200-day average slope."),
    })

    # ---------------------------------------------------------- downgrade 3
    falling = ratio_now is not None and ratio_5d_ago is not None and ratio_now < ratio_5d_ago
    below = ratio_now is not None and ratio_now < UPDOWN_VOL_DOWNGRADE_THRESH
    triggers.append({
        "id": "updown_vol_ratio_below_0.6_falling", "direction": "downgrade",
        "metric": "10-day up/down volume ratio, below 0.6 and falling vs. 5 sessions ago",
        "operator": "<", "threshold": UPDOWN_VOL_DOWNGRADE_THRESH, "current": ratio_now,
        "met": bool(below and falling),
        "plain": (f"10-day up/down volume ratio ({ratio_now:.2f}) is below 0.6 and still falling."
                  if below and falling else
                  "10-day up/down volume ratio isn't both below 0.6 and falling."),
    })

    # ---------------------------------------------------------- downgrade 4
    roll_high = close.rolling(252, min_periods=60).max()
    cur_dd_pct = None
    if pd.notna(roll_high.iloc[-1]) and roll_high.iloc[-1] > 0:
        cur_dd_pct = round(float(close.iloc[-1] / roll_high.iloc[-1] - 1) * 100, 1)
    p25 = None
    if bottom_scenarios_result and bottom_scenarios_result.get("available"):
        ladder = bottom_scenarios_result.get("ladder") or []
        tranche2 = next((t for t in ladder if t.get("tranche") == 2), None)
        p25 = tranche2.get("return_pct") if tranche2 else None
    beyond = cur_dd_pct is not None and p25 is not None and cur_dd_pct < p25
    triggers.append({
        "id": "drawdown_beyond_mc_p25", "direction": "downgrade",
        "metric": "current drawdown from 252-day high vs. Monte Carlo p25 trough estimate",
        "operator": "<", "threshold": p25, "current": cur_dd_pct,
        "met": bool(beyond),
        "plain": (f"Drawdown ({cur_dd_pct:+.1f}%) has deepened past the simulated "
                  f"25th-percentile trough ({p25:+.1f}%)." if beyond else
                  "Drawdown hasn't breached the Monte Carlo p25 trough estimate."
                  if p25 is not None else
                  "Monte Carlo trough estimate not available (not currently in a dip)."),
    })

    # ---------------------------------------------------------- downgrade 5
    rr_long = _rolling_rel_return(rel, RS_LONG_WINDOW).dropna()
    new_6mo_low = False
    if len(rr_long) >= RS_LONG_LOOKBACK:
        window = rr_long.tail(RS_LONG_LOOKBACK)
        new_6mo_low = bool(window.iloc[-1] <= window.min())
    triggers.append({
        "id": "rs_long_new_6mo_low", "direction": "downgrade",
        "metric": f"{RS_LONG_WINDOW}-day relative-return (vs SPY) makes a new {RS_LONG_LOOKBACK}-session low",
        "operator": "==", "threshold": True, "current": new_6mo_low,
        "met": new_6mo_low,
        "plain": (f"{RS_LONG_WINDOW}-day relative strength vs SPY just made a fresh 6-month low."
                  if new_6mo_low else
                  f"{RS_LONG_WINDOW}-day relative strength vs SPY hasn't made a new 6-month low."),
    })

    return triggers
