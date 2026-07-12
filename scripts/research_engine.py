#!/usr/bin/env python3
"""
Research Engine v2 — Rotation Radar.

Generalizes the extension-overlay study into an asset-agnostic evidence
machine. For every asset in scripts/universe_config.json it computes:

  TRIGGERS (what counts as a setup)
    ext200     price >= 30% above 200dma after a >= 40% six-month rally
    strength   price within 2% of its 20-day high after a >= 30% rally
               ("buying strength" as distinct from "buying extension")

  SAMPLING (the start-date-sensitivity fix)
    all        every qualifying day, 63-day cooldown  (legacy; autocorrelated)
    uptrend    first qualifying day per continuous stretch above the 200dma
               (independent episodes; the honest n)
    cross      the day the condition flips false -> true

  REGIMES at trigger (computed, not hand-tagged, except revisions)
    revisions  rising / rolling / unknown  (hand-tagged config; semis only)
    vix        calm (<20) / elevated (20-30) / stressed (>30)
    credit     narrowing / flat / widening  (HY OAS 63-day change, FRED)
    spy_trend  above / below SPY's own 200dma

  CLAIMS ("isms") — conjunction mining with a multiple-comparisons defense.
    Every regime-value conjunction (depth 1-2) with n >= 4 independent
    episodes is tested for one-sided statements ("never returned less than
    X%"). Each claim carries a COMPUTED confidence score:
      score = 100 * min(1, n/12)                 sample size
                  * consistency                  share of episodes agreeing
                  * 0.85^depth                   conjunction-depth penalty
                  * decades_covered/4 (cap 1)    regime-coverage breadth
    The digest also records how many conjunctions were searched in total,
    so the synthesis layer can reason about mining risk explicitly.

  EVIDENCE ATTACHMENTS
    Dated news headlines + average tone from GDELT for episodes after
    2017-01-01 (GDELT's reliable coverage floor). Strictly fail-soft:
    network errors or rate limits leave episodes numbers-only. Episodes
    before 2017 are explicitly marked news_available = false — we do NOT
    backfill history with model memory; that would be hindsight dressed
    as foresight.

  INDICATOR SNAPSHOTS per asset at today/-1m/-3m/-6m/-1y:
    price, RSI(14), MACD histogram, 50/200dma extension, distance from
    20-day high, and current trailing P/E where the asset has one.

Outputs:
  data/research_evidence.json   full matrix (PM layer drill-down)
  data/research_digest.json     page + PM-call input
"""

import json
import math
import os
import sys
import urllib.request
from datetime import datetime, timezone

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
UNIVERSE_PATH = os.path.join(HERE, "universe_config.json")
REGIME_PATH = os.path.join(HERE, "regime_config.json")
EVIDENCE_PATH = os.path.join(ROOT, "data", "research_evidence.json")
DIGEST_PATH = os.path.join(ROOT, "data", "research_digest.json")

FWD_DAYS = 126
PATH_STRIDE = 5
COOLDOWN = 63
NEWS_FLOOR = pd.Timestamp("2017-01-01")
NEWS_CALL_CAP = 30          # hard cap on GDELT calls per run
FRED_HY_OAS = ("https://fred.stlouisfed.org/graph/fredgraph.csv"
               "?id=BAMLH0A0HYM2")

TRIGGERS = {
    "ext200":   {"label": "Extension >30% over 200dma",
                 "desc": "≥30% above the 200-day after a ≥40% 6-mo rally"},
    "strength": {"label": "Buying strength near highs",
                 "desc": "within 2% of the 20-day high after a ≥30% 6-mo rally"},
}
SAMPLINGS = {
    "all":     "every qualifying day (63d cooldown) — autocorrelated",
    "uptrend": "one per uptrend — independent episodes",
    "cross":   "first day the condition turns true",
}

# ------------------------------------------------------------------ data


def fetch_history(symbol: str) -> pd.Series:
    import yfinance as yf
    df = yf.download(symbol, period="max", interval="1d",
                     auto_adjust=True, progress=False)
    if df is None or df.empty:
        raise RuntimeError(f"No data for {symbol}")
    close = df["Close"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    close = close.dropna()
    close.index = pd.to_datetime(close.index).tz_localize(None)
    return close


def fetch_trailing_pe(symbol: str):
    try:
        import yfinance as yf
        pe = yf.Ticker(symbol).info.get("trailingPE")
        return round(float(pe), 1) if pe else None
    except Exception:
        return None


def fetch_hy_oas() -> pd.Series:
    """FRED HY OAS, keyless CSV endpoint. Fail-soft -> empty series."""
    try:
        df = pd.read_csv(FRED_HY_OAS)
        df.columns = ["date", "oas"]
        df["date"] = pd.to_datetime(df["date"])
        df["oas"] = pd.to_numeric(df["oas"], errors="coerce")
        return df.dropna().set_index("date")["oas"]
    except Exception as e:
        print(f"[warn] FRED HY OAS unavailable: {e}")
        return pd.Series(dtype=float)


# ------------------------------------------------------------- indicators


def rsi(close: pd.Series, n=14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn
    return 100 - 100 / (1 + rs)


def indicators(close: pd.Series) -> pd.DataFrame:
    df = pd.DataFrame({"close": close})
    df["ma50"] = close.rolling(50).mean()
    df["ma200"] = close.rolling(200).mean()
    df["ext200"] = close / df["ma200"] - 1
    df["trail126"] = close / close.shift(126) - 1
    df["hi20"] = close.rolling(20).max()
    df["from_hi20"] = close / df["hi20"] - 1
    df["rsi14"] = rsi(close)
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    df["macd_hist"] = macd - macd.ewm(span=9, adjust=False).mean()
    return df


def snapshot_row(df: pd.DataFrame, offset: int):
    if len(df) <= offset:
        return None
    r = df.iloc[-1 - offset]
    if pd.isna(r["ma200"]):
        return None
    return {
        "price": round(float(r["close"]), 2),
        "rsi14": round(float(r["rsi14"]), 1),
        "macd_hist": round(float(r["macd_hist"]), 2),
        "ext200_pct": round(float(r["ext200"]) * 100, 1),
        "from_hi20_pct": round(float(r["from_hi20"]) * 100, 1),
    }


# ------------------------------------------------------- regime machinery


def load_json(path):
    with open(path) as f:
        return json.load(f)


def revision_tag(date, periods):
    for p in periods:
        if p["start"] <= date <= p["end"]:
            return p["tag"]
    return "unknown"


def vix_regime(date, vix: pd.Series):
    if vix.empty:
        return "unknown"
    s = vix.loc[:date]
    if s.empty:
        return "unknown"
    v = float(s.iloc[-1])
    return "calm" if v < 20 else ("elevated" if v <= 30 else "stressed")


def credit_regime(date, oas: pd.Series):
    if oas.empty:
        return "unknown"
    s = oas.loc[:date]
    if len(s) < 64:
        return "unknown"
    chg = float(s.iloc[-1] - s.iloc[-64])
    return "widening" if chg > 0.25 else ("narrowing" if chg < -0.25 else "flat")


def spy_trend_regime(date, spy_df: pd.DataFrame):
    s = spy_df.loc[:date]
    if s.empty or pd.isna(s["ma200"].iloc[-1]):
        return "unknown"
    return "above" if s["close"].iloc[-1] >= s["ma200"].iloc[-1] else "below"


# ------------------------------------------------------- episode detection


def trigger_mask(df: pd.DataFrame, trigger: str) -> pd.Series:
    if trigger == "ext200":
        return (df["ext200"] >= 0.30) & (df["trail126"] >= 0.40)
    if trigger == "strength":
        return (df["from_hi20"] >= -0.02) & (df["trail126"] >= 0.30)
    raise ValueError(trigger)


def sample_positions(df: pd.DataFrame, mask: pd.Series, method: str) -> list:
    hits = [i for i, h in enumerate(mask.values) if h]
    if method == "all":
        out, last = [], -10**9
        for i in hits:
            if i - last >= COOLDOWN:
                out.append(i)
                last = i
        return out
    if method == "cross":
        vals = mask.values
        return [i for i in hits if i > 0 and not vals[i - 1]]
    if method == "uptrend":
        above = (df["close"] >= df["ma200"]).values
        out, seen_this_uptrend = [], False
        for i in range(len(df)):
            if not above[i]:
                seen_this_uptrend = False
                continue
            if mask.values[i] and not seen_this_uptrend:
                out.append(i)
                seen_this_uptrend = True
        return out
    raise ValueError(method)


def build_episode(df, i, regimes_fn):
    entry = float(df["close"].iloc[i])
    end = min(i + FWD_DAYS, len(df) - 1)
    fwd = df["close"].iloc[i:end + 1] / entry * 100.0
    path = [round(float(fwd.iloc[j]), 2) for j in range(0, len(fwd), PATH_STRIDE)]
    date = df.index[i]

    def fr(n):
        j = i + n
        return (round(float(df["close"].iloc[j] / entry - 1) * 100, 2)
                if j < len(df) else None)

    return {
        "date": date.strftime("%Y-%m-%d"),
        "ext200_pct": round(float(df["ext200"].iloc[i]) * 100, 1),
        "trail_6m_pct": round(float(df["trail126"].iloc[i]) * 100, 1),
        "regimes": regimes_fn(date),
        "fwd_63d_pct": fr(63),
        "fwd_126d_pct": fr(126),
        "max_dd_pct": round(float(fwd.min() - 100), 2),
        "complete": (i + FWD_DAYS) <= (len(df) - 1),
        "path": path,
        "news_available": bool(date >= NEWS_FLOOR),
        "news": None,
    }


# ------------------------------------------------------------ news layer


class NewsBudget:
    def __init__(self, cap):
        self.left = cap


def attach_news(ep: dict, query: str, budget: NewsBudget):
    """Dated GDELT headlines + tone for the trigger week. Fail-soft."""
    if not ep["news_available"] or budget.left <= 0:
        return
    budget.left -= 1
    d = ep["date"].replace("-", "")
    url = ("https://api.gdeltproject.org/api/v2/doc/doc?"
           f"query={urllib.request.quote(query)}&mode=artlist&maxrecords=5"
           f"&format=json&startdatetime={d}000000&enddatetime={d}235959")
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            arts = json.load(r).get("articles", [])
        ep["news"] = [{"title": a.get("title", "")[:140],
                       "date": ep["date"],
                       "source": a.get("domain", "")} for a in arts[:5]]
    except Exception:
        ep["news"] = None  # numbers-only; never backfilled from memory


# --------------------------------------------------------- claim mining


def median(vals):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    n, m = len(vals), len(vals) // 2
    return round(vals[m] if n % 2 else (vals[m - 1] + vals[m]) / 2, 2)


def confidence(n, consistency, depth, decades):
    score = 100 * min(1, n / 12) * consistency * (0.85 ** depth) \
        * min(1, decades / 4)
    score = round(score)
    label = "high" if score >= 70 else ("moderate" if score >= 40
                                        else "low / likely mined")
    return score, label


def mine_claims(episodes, regime_keys):
    """Depth-1 and depth-2 regime conjunctions over independent episodes."""
    done = [e for e in episodes if e["complete"]]
    claims, searched = [], 0

    def conjunctions():
        for k in regime_keys:
            for v in {e["regimes"][k] for e in done}:
                yield ((k, v),)
        keys = list(regime_keys)
        for a in range(len(keys)):
            for b in range(a + 1, len(keys)):
                vas = {e["regimes"][keys[a]] for e in done}
                vbs = {e["regimes"][keys[b]] for e in done}
                for va in vas:
                    for vb in vbs:
                        yield ((keys[a], va), (keys[b], vb))

    for conj in conjunctions():
        searched += 1
        subset = [e for e in done
                  if all(e["regimes"][k] == v for k, v in conj)]
        if len(subset) < 4:
            continue
        rets = [e["fwd_126d_pct"] for e in subset if e["fwd_126d_pct"] is not None]
        if not rets:
            continue
        med = median(rets)
        pos = [r for r in rets if r > 0]
        consistency = max(len(pos), len(rets) - len(pos)) / len(rets)
        decades = len({e["date"][:3] for e in subset})
        score, label = confidence(len(subset), consistency, len(conj), decades)
        worst_ret = min(rets)
        worst_dd = min(e["max_dd_pct"] for e in subset)
        exceptions = [e["date"] for e in subset
                      if e["fwd_126d_pct"] is not None and
                      (e["fwd_126d_pct"] < 0) == (med >= 0)][:4]
        claims.append({
            "conditions": [f"{k}={v}" for k, v in conj],
            "n": len(subset),
            "median_fwd_126d_pct": med,
            "floor_fwd_126d_pct": worst_ret,
            "worst_interim_dd_pct": worst_dd,
            "pct_agreeing": round(consistency * 100),
            "decades_covered": decades,
            "confidence": score,
            "confidence_label": label,
            "exceptions": exceptions,
        })
    claims.sort(key=lambda c: -c["confidence"])
    return claims, searched


# --------------------------------------------------------------- assembly


def run(universe, price_map, spy_df, vix, oas, rev_periods,
        pe_map=None, news_budget=None):
    pe_map = pe_map or {}
    news_budget = news_budget or NewsBudget(0)
    evidence, digest_assets = {}, {}

    for asset in universe["assets"]:
        t = asset["ticker"]
        if t not in price_map:
            continue
        df = indicators(price_map[t])

        def regimes_fn(date, _asset=asset):
            out = {
                "vix": vix_regime(date, vix),
                "credit": credit_regime(date, oas),
                "spy_trend": spy_trend_regime(date, spy_df),
            }
            if _asset.get("use_revision_tags"):
                out["revisions"] = revision_tag(date, rev_periods)
            return out

        regime_keys = ["vix", "credit", "spy_trend"] \
            + (["revisions"] if asset.get("use_revision_tags") else [])

        studies = {}
        for trig in TRIGGERS:
            mask = trigger_mask(df, trig)
            for samp in SAMPLINGS:
                eps = [build_episode(df, i, regimes_fn)
                       for i in sample_positions(df, mask, samp)]
                if samp == "uptrend":  # attach news only to the honest sample
                    for e in eps:
                        attach_news(e, asset.get("query", t), news_budget)
                claims, searched = mine_claims(eps, regime_keys)
                done = [e for e in eps if e["complete"]]
                studies[f"{trig}|{samp}"] = {
                    "episodes": eps,
                    "claims": claims,
                    "conjunctions_searched": searched,
                    "summary": {
                        "n": len(eps), "n_complete": len(done),
                        "median_fwd_126d_pct":
                            median([e["fwd_126d_pct"] for e in done]),
                        "median_max_dd_pct":
                            median([e["max_dd_pct"] for e in done]),
                        "worst_max_dd_pct":
                            min((e["max_dd_pct"] for e in done), default=None),
                        "pct_positive_126d":
                            (round(100 * len([e for e in done
                                              if (e["fwd_126d_pct"] or 0) > 0])
                                   / len(done)) if done else None),
                    },
                }

        snaps = {lbl: snapshot_row(df, off) for lbl, off in
                 [("today", 0), ("1m", 21), ("3m", 63),
                  ("6m", 126), ("1y", 252)]}
        current = {
            "as_of": df.index[-1].strftime("%Y-%m-%d"),
            "snapshots": snaps,
            "trailing_pe": pe_map.get(t),
            "regimes": regimes_fn(df.index[-1]),
            "triggers_active": {trig: bool(trigger_mask(df, trig).iloc[-1])
                                for trig in TRIGGERS},
        }
        evidence[t] = {"label": asset["label"], "studies": studies,
                       "current": current}
        digest_assets[t] = {
            "label": asset["label"], "current": current,
            "studies": {k: {"summary": v["summary"],
                            "claims": v["claims"][:8],
                            "conjunctions_searched": v["conjunctions_searched"],
                            "episodes": [{kk: e[kk] for kk in
                                          ("date", "regimes", "fwd_126d_pct",
                                           "max_dd_pct", "complete", "path",
                                           "news")}
                                         for e in v["episodes"]]}
                        for k, v in studies.items()},
        }

    meta = {
        "generated_utc": datetime.now(timezone.utc)
            .strftime("%Y-%m-%d %H:%M UTC"),
        "triggers": TRIGGERS, "samplings": SAMPLINGS,
        "caveats": [
            "'all' sampling is autocorrelated — the same rally counts many "
            "times. Use 'uptrend' for honest, independent n.",
            "Revision tags are hand-audited (semis only) and lag at "
            "inflections. VIX / credit / SPY-trend regimes are computed.",
            "Claims are mined from many conjunctions; the searched count is "
            "recorded so low-confidence 'never happened' claims can be "
            "treated as noise.",
            "News is attached only from dated archives (2017+). Older "
            "episodes are numbers-only by design — no retrospective "
            "narratives.",
            "Historical valuation series are not available free; trailing "
            "P/E is current-only and N/A for non-equity assets.",
        ],
    }
    return ({"meta": meta, "assets": evidence},
            {"meta": meta, "assets": digest_assets})


def main():
    universe = load_json(UNIVERSE_PATH)
    rev_periods = [{"start": pd.Timestamp(p["start"]),
                    "end": pd.Timestamp(p["end"]), "tag": p["tag"]}
                   for p in load_json(REGIME_PATH)["periods"]]

    price_map, pe_map = {}, {}
    for a in universe["assets"]:
        try:
            price_map[a["ticker"]] = fetch_history(a["ticker"])
            if a.get("valuation"):
                pe_map[a["ticker"]] = fetch_trailing_pe(a["ticker"])
        except Exception as e:
            print(f"[warn] skipping {a['ticker']}: {e}")
    if "SPY" not in price_map:
        price_map["SPY"] = fetch_history("SPY")
    spy_df = indicators(price_map["SPY"])
    try:
        vix = fetch_history("^VIX")
    except Exception:
        vix = pd.Series(dtype=float)
    oas = fetch_hy_oas()

    evidence, digest = run(universe, price_map, spy_df, vix, oas,
                           rev_periods, pe_map, NewsBudget(NEWS_CALL_CAP))
    os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
    with open(EVIDENCE_PATH, "w") as f:
        json.dump(evidence, f)
    with open(DIGEST_PATH, "w") as f:
        json.dump(digest, f)
    for t, a in digest["assets"].items():
        s = a["studies"].get("ext200|uptrend", {}).get("summary", {})
        print(f"{t}: uptrend-sampled ext200 episodes={s.get('n')} "
              f"median126={s.get('median_fwd_126d_pct')}")


if __name__ == "__main__":
    sys.exit(main())
