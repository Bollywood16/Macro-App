"""
episodes.py — "Episodes like this" discovery engine.

This is the piece that removes the dependence on already knowing which past
events to ask about. It SCREENS the asset's own history for days whose
statistical fingerprint matches today (drawdown depth, extension, stretch),
returns those dates + how they resolved, and merges any causal narrative
already stored in the episode library (populated from handoff research).

Division of labor:
  * FIND + QUANTIFY the analogs      -> this module (pure Python).
  * EXPLAIN cause / what ended them  -> LLM via handoff, then written back
                                        to the `episodes` table and merged here.

Input: OHLCV DataFrame + optional list of stored library rows.
Output: JSON-serializable dict -> EpisodesCard + handoff-prompt builder.
"""
from __future__ import annotations
import numpy as np
import pandas as pd

FWD = 126
SPACING = 126
MATCH_TOL = {"drawdown": 0.06, "extension": 0.05}   # how close counts as "like this"


def _fingerprint(df: pd.DataFrame):
    close = df["close"]
    ma200 = close.rolling(200).mean()
    roll_high = close.rolling(252, min_periods=60).max()
    dd = close / roll_high - 1
    ext = close / ma200 - 1
    return dd, ext


def find_analogs(df: pd.DataFrame, library: list[dict] | None = None) -> dict:
    close = df["close"]
    dd, ext = _fingerprint(df)
    cur_dd, cur_ext = dd.iloc[-1], ext.iloc[-1]

    ma50 = close.rolling(50).mean(); ma200 = close.rolling(200).mean()
    intact = (ma50 > ma200) & (ma200 > ma200.shift(21))

    match = (
        (dd - cur_dd).abs() <= MATCH_TOL["drawdown"]
    ) & (
        (ext - cur_ext).abs() <= MATCH_TOL["extension"]
    ) & intact

    lib = {row["date"]: row for row in (library or [])}
    episodes, last = [], -SPACING
    for pos in np.flatnonzero(match.values):
        if pos - last < SPACING or pos + FWD >= len(close):
            continue
        entry = close.iloc[pos]
        fwd = close.iloc[pos:pos + FWD + 1]
        date = str(close.index[pos].date())
        ep = {
            "date": date,
            "drawdown_pct": round(float(dd.iloc[pos] * 100), 1),
            "fwd_126d_pct": round(float((fwd.iloc[-1] / entry - 1) * 100), 1),
            "worst_interim_pct": round(float((fwd.min() / entry - 1) * 100), 1),
            "days_to_trough": int(np.argmin(fwd.values)),
            # merged causal narrative if the library already has it:
            "cause": lib.get(date, {}).get("cause"),
            "what_ended_it": lib.get(date, {}).get("what_ended_it"),
            "annotated": date in lib,
        }
        episodes.append(ep)
        last = pos

    n = len(episodes)
    up = sum(1 for e in episodes if e["fwd_126d_pct"] > 0)
    plain = ([f"Found {n} past setups statistically like today. "
              f"{up} of {n} were higher 6 months later."]
             if n else ["No close statistical analogs found in this asset's history."])
    unannotated = [e["date"] for e in episodes if not e["annotated"]]
    return {"n": n, "episodes": episodes, "plain": plain,
            "unannotated_dates": unannotated}


def build_handoff_prompt(ticker: str, result: dict) -> str:
    """Pre-writes the research question for the handoff, so the user never has
    to know which events to ask about — Python found the dates, this asks why."""
    dates = result.get("unannotated_dates") or [e["date"] for e in result["episodes"]]
    if not dates:
        return ""
    lines = "\n".join(
        f"- {e['date']}: drawdown {e['drawdown_pct']}%, "
        f"resolved {e['fwd_126d_pct']:+}% over 6 months"
        for e in result["episodes"] if e["date"] in dates)
    return (
        f"My system flagged these dates as statistically similar to today's setup in {ticker}:\n"
        f"{lines}\n\n"
        f"For each date, research what CAUSED the drawdown and what ENDED it "
        f"(the catalyst that turned it around). Then tell me which past cause most "
        f"resembles the present situation, and what that implies. Be concise and "
        f"cite sources; flag any date where the cause is ambiguous."
    )
