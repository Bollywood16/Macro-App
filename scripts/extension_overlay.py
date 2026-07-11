#!/usr/bin/env python3
"""
Extension Overlay Study — Rotation Radar feature module.

Question under test: after a major rally, does buying semis at an extended
distance above the 200-day moving average beat waiting for a pullback —
and does the answer depend on the earnings-revision regime at trigger time?

Method:
  1. Pull full price history for ^SOX (index, longest history) and SMH.
  2. Find "trigger days": price >= EXT_THRESHOLD above the 200dma AND
     trailing RALLY_LOOKBACK return >= RALLY_MIN, with a cooldown so
     clustered days count as one episode.
  3. For each episode, record the forward FWD_DAYS path normalized to 100
     at trigger, the max interim drawdown, and 63d / 126d forward returns.
  4. Tag each episode with the earnings-revision regime from
     regime_config.json ("rising" / "rolling" / "unknown"). Tags are a
     hand-audited reconstruction — edit the config as your view improves.
  5. Compute per-cohort stats + median paths, and the current SMH status
     with its nearest historical analogs.
  6. Write everything to data/extension_overlay.json for the PWA page.

Run locally:  python scripts/extension_overlay.py
Run in CI:    .github/workflows/extension-overlay.yml (weekly + manual)
"""

import json
import math
import os
import sys
from datetime import datetime, timezone

import pandas as pd

# ---------------------------------------------------------------- parameters

EXT_THRESHOLD = 0.30      # price must be >= 30% above its 200dma
RALLY_LOOKBACK = 126      # trading days (~6 months) for the trailing rally
RALLY_MIN = 0.40          # trailing rally must be >= +40%
COOLDOWN = 63             # trading days between distinct episodes (~3 months)
FWD_DAYS = 126            # forward window (~6 months)
PATH_STRIDE = 5           # sample forward path weekly to keep JSON small
MA_WINDOW = 200

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
CONFIG_PATH = os.path.join(HERE, "regime_config.json")
OUTPUT_PATH = os.path.join(REPO_ROOT, "data", "extension_overlay.json")

# ---------------------------------------------------------------- data layer


def fetch_history(symbol: str) -> pd.Series:
    """Full daily close history for a symbol via yfinance. Returns a Series
    indexed by date. Raises on empty data so CI fails loudly, not silently."""
    import yfinance as yf

    df = yf.download(symbol, period="max", interval="1d",
                     auto_adjust=True, progress=False)
    if df is None or df.empty:
        raise RuntimeError(f"No data returned for {symbol}")
    close = df["Close"]
    if isinstance(close, pd.DataFrame):  # yfinance MultiIndex quirk
        close = close.iloc[:, 0]
    close = close.dropna()
    close.index = pd.to_datetime(close.index).tz_localize(None)
    return close


# ------------------------------------------------------------ regime tagging


def load_regime_periods(path: str = CONFIG_PATH) -> list:
    with open(path) as f:
        cfg = json.load(f)
    periods = []
    for p in cfg["periods"]:
        periods.append({
            "start": pd.Timestamp(p["start"]),
            "end": pd.Timestamp(p["end"]),
            "tag": p["tag"],
            "note": p.get("note", ""),
        })
    return periods


def regime_for(date: pd.Timestamp, periods: list) -> str:
    for p in periods:
        if p["start"] <= date <= p["end"]:
            return p["tag"]
    return "unknown"


# ---------------------------------------------------------- episode detection


def compute_indicators(close: pd.Series) -> pd.DataFrame:
    df = pd.DataFrame({"close": close})
    df["ma200"] = df["close"].rolling(MA_WINDOW).mean()
    df["ext"] = df["close"] / df["ma200"] - 1.0
    df["trail"] = df["close"] / df["close"].shift(RALLY_LOOKBACK) - 1.0
    return df


def detect_triggers(df: pd.DataFrame) -> list:
    """Return integer positions of episode trigger days."""
    cond = (df["ext"] >= EXT_THRESHOLD) & (df["trail"] >= RALLY_MIN)
    positions, last = [], -10**9
    idx_positions = [i for i, hit in enumerate(cond.values) if hit]
    for i in idx_positions:
        if i - last >= COOLDOWN:
            positions.append(i)
            last = i
    return positions


def build_episode(df: pd.DataFrame, i: int, symbol: str,
                  periods: list) -> dict:
    dates = df.index
    entry = float(df["close"].iloc[i])
    fwd_end = min(i + FWD_DAYS, len(df) - 1)
    fwd = df["close"].iloc[i:fwd_end + 1] / entry * 100.0

    # Sampled path (day 0, then every PATH_STRIDE days)
    path = [round(float(fwd.iloc[j]), 2)
            for j in range(0, len(fwd), PATH_STRIDE)]
    if (len(fwd) - 1) % PATH_STRIDE != 0:
        path.append(round(float(fwd.iloc[-1]), 2))

    max_dd = round(float(fwd.min() - 100.0), 2)  # worst point vs entry, in %
    complete = (i + FWD_DAYS) <= (len(df) - 1)

    def fwd_ret(n):
        j = i + n
        if j > len(df) - 1:
            return None
        return round(float(df["close"].iloc[j] / entry - 1.0) * 100.0, 2)

    return {
        "date": dates[i].strftime("%Y-%m-%d"),
        "symbol": symbol,
        "ext_pct": round(float(df["ext"].iloc[i]) * 100.0, 1),
        "trail_6m_pct": round(float(df["trail"].iloc[i]) * 100.0, 1),
        "regime": regime_for(dates[i], periods),
        "fwd_63d_pct": fwd_ret(63),
        "fwd_126d_pct": fwd_ret(126),
        "max_dd_pct": max_dd,
        "complete": complete,
        "path": path,
    }


# ----------------------------------------------------------------- statistics


def median(vals):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    n = len(vals)
    mid = n // 2
    return round(vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2, 2)


def cohort_stats(episodes: list) -> dict:
    out = {}
    for tag in ("rising", "rolling", "unknown"):
        subset = [e for e in episodes if e["regime"] == tag]
        done = [e for e in subset if e["complete"]]
        r126 = [e["fwd_126d_pct"] for e in done]
        positives = [r for r in r126 if r is not None and r > 0]
        out[tag] = {
            "n": len(subset),
            "n_complete": len(done),
            "median_fwd_63d_pct": median([e["fwd_63d_pct"] for e in done]),
            "median_fwd_126d_pct": median(r126),
            "median_max_dd_pct": median([e["max_dd_pct"] for e in done]),
            "worst_max_dd_pct": (min((e["max_dd_pct"] for e in done),
                                     default=None)),
            "pct_positive_126d": (round(100 * len(positives) / len(done), 0)
                                  if done else None),
        }
    return out


def median_path(episodes: list) -> list:
    done = [e["path"] for e in episodes if e["complete"]]
    if not done:
        return []
    length = min(len(p) for p in done)
    return [median([p[j] for p in done]) for j in range(length)]


# ------------------------------------------------------------- current status


def current_status(smh: pd.DataFrame, episodes: list, periods: list) -> dict:
    last = smh.dropna().iloc[-1]
    last_date = smh.dropna().index[-1]
    ext = float(last["ext"])
    trail = float(last["trail"])
    tag = regime_for(last_date, periods)

    # Nearest analogs by Euclidean distance in (ext, trailing return) space
    def dist(e):
        return math.sqrt((e["ext_pct"] - ext * 100) ** 2
                         + (e["trail_6m_pct"] - trail * 100) ** 2)

    analogs = sorted((e for e in episodes if e["complete"]), key=dist)[:3]

    return {
        "as_of": last_date.strftime("%Y-%m-%d"),
        "symbol": "SMH",
        "close": round(float(last["close"]), 2),
        "ma200": round(float(last["ma200"]), 2),
        "ext_pct": round(ext * 100, 1),
        "trail_6m_pct": round(trail * 100, 1),
        "trigger_active": bool(ext >= EXT_THRESHOLD and trail >= RALLY_MIN),
        "regime_tag": tag,
        "nearest_analogs": [
            {"date": a["date"], "symbol": a["symbol"], "regime": a["regime"],
             "ext_pct": a["ext_pct"], "fwd_126d_pct": a["fwd_126d_pct"],
             "max_dd_pct": a["max_dd_pct"]}
            for a in analogs
        ],
    }


# ------------------------------------------------------------------- assembly


def run(sox: pd.Series, smh: pd.Series, periods: list) -> dict:
    """Pure assembly step — takes price series, returns the output dict.
    Kept free of network calls so it can be tested with synthetic data."""
    sox_df = compute_indicators(sox)
    smh_df = compute_indicators(smh)

    # Detect on ^SOX for maximum history; add SMH-only episodes after the
    # index history ends or if SOX data is unavailable for a period.
    episodes = [build_episode(sox_df, i, "SOX", periods)
                for i in detect_triggers(sox_df)]

    sox_dates = {e["date"][:7] for e in episodes}
    for i in detect_triggers(smh_df):
        month = smh_df.index[i].strftime("%Y-%m")
        if month not in sox_dates:  # avoid double-counting the same event
            episodes.append(build_episode(smh_df, i, "SMH", periods))
    episodes.sort(key=lambda e: e["date"])

    rising = [e for e in episodes if e["regime"] == "rising"]
    rolling = [e for e in episodes if e["regime"] == "rolling"]

    return {
        "meta": {
            "generated_utc": datetime.now(timezone.utc)
                .strftime("%Y-%m-%d %H:%M UTC"),
            "params": {
                "ext_threshold_pct": EXT_THRESHOLD * 100,
                "rally_lookback_days": RALLY_LOOKBACK,
                "rally_min_pct": RALLY_MIN * 100,
                "cooldown_days": COOLDOWN,
                "fwd_days": FWD_DAYS,
                "path_stride_days": PATH_STRIDE,
            },
            "sox_history_start": sox.index[0].strftime("%Y-%m-%d"),
            "smh_history_start": smh.index[0].strftime("%Y-%m-%d"),
            "caveats": [
                "Regime tags are a hand-audited reconstruction of forward "
                "EPS-revision direction; edit scripts/regime_config.json.",
                "^SOX is a price index (no dividends); path shapes are the "
                "object of study, not total returns.",
                "Episode counts are small. Treat cohort medians as priors, "
                "not guarantees.",
            ],
        },
        "current": current_status(smh_df, episodes, periods),
        "cohorts": cohort_stats(episodes),
        "median_paths": {
            "rising": median_path(rising),
            "rolling": median_path(rolling),
        },
        "episodes": episodes,
    }


def main():
    periods = load_regime_periods()
    sox = fetch_history("^SOX")
    smh = fetch_history("SMH")
    result = run(sox, smh, periods)
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(result, f, indent=1)
    print(f"Wrote {OUTPUT_PATH}: {len(result['episodes'])} episodes "
          f"({result['cohorts']['rising']['n']} rising / "
          f"{result['cohorts']['rolling']['n']} rolling / "
          f"{result['cohorts']['unknown']['n']} unknown). "
          f"Current SMH ext: {result['current']['ext_pct']}%, "
          f"trigger_active={result['current']['trigger_active']}")


if __name__ == "__main__":
    sys.exit(main())
