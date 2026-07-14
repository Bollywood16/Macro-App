#!/usr/bin/env python3
"""
Rotation Engine — Rotation Radar.

Relative-performance layer. Grounded in the literature's split verdict:
industry momentum is real (Moskowitz-Grinblatt 1999), but mechanical
business-cycle sector rotation mostly fails after costs (Stangl-Jacobsen-
Visaltanachoti). So this engine treats LEADERSHIP AS EVIDENCE, not as a
sector-picking strategy: it classifies each historical day's leadership
regime and mines conditional claims about what followed for the index.

Computes:
  1. RELATIVE STRENGTH table: each sector/size ETF vs SPY over
     21/63/126/252 trading days, ranked.
  2. LEADERSHIP REGIME per day: defensive-led / cyclical-led /
     megacap-led / smallcap-led / broad — the group with the largest
     63-day return spread vs SPY beyond a threshold.
  3. CONDITIONAL CLAIMS (the user's healthcare/financials pattern,
     generalized): leadership regime × rates direction × VIX regime ×
     credit direction (depth <= 2) -> forward SPY 63d return stats +
     which sector led over the FOLLOWING quarter. Same computed
     confidence and searched-count discipline as the research engine.
     Episodes are sampled one-per-regime-onset (first day of each new
     leadership regime lasting >= 10 days) to avoid autocorrelation.
  4. RELATIVE CURVES for the comparison chart: cumulative return vs SPY
     over the trailing year, sampled every 3 days.

Declared proxies (no free history exists for the real thing):
  - "vix" regime stands in for the CNN Fear & Greed index.
  - "rates" = 10-year Treasury yield 63-day direction (^TNX), standing
    in for rate-hike/cut expectations.

Output: data/rotation_digest.json
"""

import json
import os
import sys
from datetime import datetime, timezone

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import regime_hmm  # noqa: E402  (data-driven comparison regime, fail-soft)

CONFIG_PATH = os.path.join(HERE, "rotation_config.json")
OUTPUT_PATH = os.path.join(ROOT, "data", "rotation_digest.json")

LOOKBACKS = {"1m": 21, "3m": 63, "6m": 126, "1y": 252}
REGIME_MIN_DAYS = 10
FWD = 63
FRED_HY_OAS = ("https://fred.stlouisfed.org/graph/fredgraph.csv"
               "?id=BAMLH0A0HYM2")


def fetch_history(symbol):
    import yfinance as yf
    df = yf.download(symbol, period="max", interval="1d",
                     auto_adjust=True, progress=False)
    if df is None or df.empty:
        raise RuntimeError(f"No data for {symbol}")
    c = df["Close"]
    if isinstance(c, pd.DataFrame):
        c = c.iloc[:, 0]
    c = c.dropna()
    c.index = pd.to_datetime(c.index).tz_localize(None)
    return c


def fetch_hy_oas():
    try:
        df = pd.read_csv(FRED_HY_OAS)
        df.columns = ["date", "oas"]
        df["date"] = pd.to_datetime(df["date"])
        df["oas"] = pd.to_numeric(df["oas"], errors="coerce")
        return df.dropna().set_index("date")["oas"]
    except Exception as e:
        print(f"[warn] FRED unavailable: {e}")
        return pd.Series(dtype=float)


# --------------------------------------------------------------- regimes


def series_regime_vix(vix, date):
    s = vix.loc[:date]
    if s.empty:
        return "unknown"
    v = float(s.iloc[-1])
    return "calm" if v < 20 else ("elevated" if v <= 30 else "stressed")


def series_regime_rates(tnx, date):
    s = tnx.loc[:date]
    if len(s) < 64:
        return "unknown"
    chg = float(s.iloc[-1] - s.iloc[-64])
    return "rising" if chg > 0.25 else ("falling" if chg < -0.25 else "flat")


def series_regime_credit(oas, date):
    s = oas.loc[:date]
    if len(s) < 64:
        return "unknown"
    chg = float(s.iloc[-1] - s.iloc[-64])
    return "widening" if chg > 0.25 else ("narrowing" if chg < -0.25
                                          else "flat")


# ------------------------------------------------------------- leadership


def build_panel(cfg, prices):
    """Aligned daily close panel of all tickers, forward-filled to the
    common calendar of the benchmark."""
    bench = cfg["benchmark"]
    idx = prices[bench].index
    data = {t: s.reindex(idx).ffill() for t, s in prices.items()}
    return pd.DataFrame(data)


def group_spread(panel, members, bench, i, n=63):
    """Average 63d return of group members (that have data) minus SPY's."""
    if i < n:
        return None
    rets = []
    for m in members:
        if m not in panel.columns:
            continue
        a, b = panel[m].iloc[i], panel[m].iloc[i - n]
        if pd.notna(a) and pd.notna(b) and b > 0:
            rets.append(a / b - 1)
    if not rets:
        return None
    bnow, bthen = panel[bench].iloc[i], panel[bench].iloc[i - n]
    return (sum(rets) / len(rets) - (bnow / bthen - 1)) * 100


def leadership_series(panel, cfg):
    bench = cfg["benchmark"]
    thr = cfg["leadership_threshold_pct"]
    labels = []
    for i in range(len(panel)):
        spreads = {g: group_spread(panel, m, bench, i)
                   for g, m in cfg["groups"].items()}
        spreads = {g: v for g, v in spreads.items() if v is not None}
        if not spreads:
            labels.append("unknown")
            continue
        g, v = max(spreads.items(), key=lambda kv: abs(kv[1]))
        if v >= thr:
            labels.append(f"{g}-led")
        elif v <= -thr:
            labels.append(f"anti-{g}")   # group lagging hardest
        else:
            labels.append("broad")
    return pd.Series(labels, index=panel.index)


def regime_onsets(lead):
    """First day of each leadership regime that persisted >= REGIME_MIN_DAYS.
    One episode per regime stretch — the autocorrelation defense."""
    out, i, vals = [], 0, lead.values
    while i < len(vals):
        j = i
        while j < len(vals) and vals[j] == vals[i]:
            j += 1
        if (j - i) >= REGIME_MIN_DAYS and vals[i] not in ("unknown",):
            out.append(i)
        i = j
    return out


# ----------------------------------------------------------- claim mining


def median(vals):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    n, m = len(vals), len(vals) // 2
    return round(vals[m] if n % 2 else (vals[m - 1] + vals[m]) / 2, 2)


def confidence(n, consistency, depth, decades):
    score = round(100 * min(1, n / 12) * consistency * (0.85 ** depth)
                  * min(1, decades / 4))
    return score, ("high" if score >= 70 else
                   "moderate" if score >= 40 else "low / likely mined")


def fwd_return(panel, t, i, n=FWD):
    if t not in panel.columns or i + n >= len(panel):
        return None
    a, b = panel[t].iloc[i + n], panel[t].iloc[i]
    if pd.isna(a) or pd.isna(b) or b <= 0:
        return None
    return round((a / b - 1) * 100, 2)


def build_episodes(panel, cfg, lead, vix, tnx, oas):
    bench = cfg["benchmark"]
    sectors = [s["ticker"] for s in cfg["sectors"]]
    eps = []
    for i in regime_onsets(lead):
        date = panel.index[i]
        fwd_bench = fwd_return(panel, bench, i)
        sector_fwd = {t: fwd_return(panel, t, i) for t in sectors}
        sector_fwd = {t: v for t, v in sector_fwd.items() if v is not None}
        best = max(sector_fwd.items(), key=lambda kv: kv[1])[0] \
            if sector_fwd else None
        eps.append({
            "date": date.strftime("%Y-%m-%d"),
            "leadership": lead.iloc[i],
            "regimes": {
                "leadership": lead.iloc[i],
                "vix": series_regime_vix(vix, date),
                "rates": series_regime_rates(tnx, date),
                "credit": series_regime_credit(oas, date),
            },
            "fwd_63d_spy_pct": fwd_bench,
            "best_fwd_sector": best,
            "complete": fwd_bench is not None,
        })
    return eps


def mine(eps):
    done = [e for e in eps if e["complete"]]
    keys = ["leadership", "vix", "rates", "credit"]
    claims, searched = [], 0

    def conjs():
        for k in keys:
            for v in {e["regimes"][k] for e in done}:
                yield ((k, v),)
        for a in range(len(keys)):
            for b in range(a + 1, len(keys)):
                for va in {e["regimes"][keys[a]] for e in done}:
                    for vb in {e["regimes"][keys[b]] for e in done}:
                        yield ((keys[a], va), (keys[b], vb))

    for conj in conjs():
        searched += 1
        sub = [e for e in done
               if all(e["regimes"][k] == v for k, v in conj)]
        if len(sub) < 6:
            continue
        rets = [e["fwd_63d_spy_pct"] for e in sub]
        med = median(rets)
        pos = [r for r in rets if r > 0]
        consistency = max(len(pos), len(rets) - len(pos)) / len(rets)
        decades = len({e["date"][:3] for e in sub})
        score, label = confidence(len(sub), consistency, len(conj), decades)
        best_counts = {}
        for e in sub:
            if e["best_fwd_sector"]:
                best_counts[e["best_fwd_sector"]] = \
                    best_counts.get(e["best_fwd_sector"], 0) + 1
        top_sector = max(best_counts.items(), key=lambda kv: kv[1])[0] \
            if best_counts else None
        claims.append({
            "conditions": [f"{k}={v}" for k, v in conj],
            "n": len(sub),
            "median_fwd_63d_spy_pct": med,
            "floor_fwd_63d_spy_pct": min(rets),
            "pct_agreeing": round(consistency * 100),
            "decades_covered": decades,
            "most_frequent_leading_sector_fwd": top_sector,
            "confidence": score, "confidence_label": label,
            "exceptions": [e["date"] for e in sub
                           if (e["fwd_63d_spy_pct"] < 0) == (med >= 0)][:4],
        })
    claims.sort(key=lambda c: -c["confidence"])
    return claims, searched


# ------------------------------------------------------------- snapshots


def rs_table(panel, cfg):
    bench = cfg["benchmark"]
    rows = []
    for item in cfg["sectors"] + cfg["size_style"]:
        t = item["ticker"]
        if t not in panel.columns:
            continue
        row = {"ticker": t, "label": item["label"]}
        ok = False
        for lbl, n in LOOKBACKS.items():
            if len(panel) <= n or pd.isna(panel[t].iloc[-1 - n]) \
                    or pd.isna(panel[t].iloc[-1]):
                row[lbl] = None
                continue
            r = panel[t].iloc[-1] / panel[t].iloc[-1 - n] - 1
            b = panel[bench].iloc[-1] / panel[bench].iloc[-1 - n] - 1
            row[lbl] = round((r - b) * 100, 1)
            ok = True
        if ok:
            rows.append(row)
    rows.sort(key=lambda r: -(r["3m"] if r["3m"] is not None else -999))
    return rows


def relative_curves(panel, cfg, days=252, stride=3):
    bench = cfg["benchmark"]
    n = min(days, len(panel) - 1)
    sub = panel.iloc[-n:]
    curves, dates = {}, [d.strftime("%Y-%m-%d")
                         for d in sub.index[::stride]]
    b0 = sub[bench].iloc[0]
    brel = sub[bench] / b0
    for item in cfg["sectors"] + cfg["size_style"]:
        t = item["ticker"]
        if t not in sub.columns or pd.isna(sub[t].iloc[0]):
            continue
        rel = (sub[t] / sub[t].iloc[0]) / brel * 100 - 100
        curves[t] = [round(float(v), 2) for v in rel.iloc[::stride]]
    return {"dates": dates, "curves": curves}




# ---------------------------------------------------------------- macro

FRED_SERIES = {
    "DGS10":  ("10-Year Treasury", "%",
               "What the government pays to borrow for 10 years. The anchor for mortgages and stock valuations — rising = tighter conditions."),
    "DGS2":   ("2-Year Treasury", "%",
               "Tracks where the market thinks the Fed is heading over the next couple of years."),
    "DFF":    ("Fed Funds Rate", "%",
               "The Fed's actual policy rate today."),
    "UNRATE": ("Unemployment Rate", "%",
               "Share of the labor force out of work. Rising off a low is the classic late-cycle warning."),
    "ICSA":   ("Initial Jobless Claims", "k",
               "People filing for unemployment for the first time each week — the fastest-updating jobs signal."),
    "CPIYOY": ("CPI Inflation (YoY)", "%",
               "How much consumer prices rose vs a year ago. What the Fed is reacting to."),
    "BAMLH0A0HYM2": ("High-Yield Spread", "%",
               "Extra yield junk-bond investors demand over Treasuries. The credit market's fear gauge — widening = stress building."),
}


def fred_series(series_id):
    try:
        df = pd.read_csv("https://fred.stlouisfed.org/graph/fredgraph.csv?id="
                         + series_id)
        df.columns = ["date", "v"]
        df["date"] = pd.to_datetime(df["date"])
        df["v"] = pd.to_numeric(df["v"], errors="coerce")
        return df.dropna().set_index("date")["v"]
    except Exception as e:
        print(f"[warn] FRED {series_id} unavailable: {e}")
        return pd.Series(dtype=float)


def value_at(s, when):
    sub = s.loc[:when]
    return float(sub.iloc[-1]) if len(sub) else None


def macro_block(vix):
    rows = []
    now = pd.Timestamp.now()

    def add(key, label, unit, explain, series):
        if series is None or series.empty:
            return
        latest = float(series.iloc[-1])
        asof = series.index[-1].strftime("%Y-%m-%d")
        v3m = value_at(series, now - pd.Timedelta(days=91))
        v1y = value_at(series, now - pd.Timedelta(days=365))
        rows.append({
            "key": key, "label": label, "unit": unit, "explain": explain,
            "value": round(latest, 2), "asof": asof,
            "chg_3m": round(latest - v3m, 2) if v3m is not None else None,
            "chg_1y": round(latest - v1y, 2) if v1y is not None else None,
        })

    if vix is not None and not vix.empty:
        add("VIX", "VIX", "", "Expected 30-day stock-market volatility. "
            "Under 20 = calm, over 30 = stressed. Stands in for the fear/"
            "greed gauge.", vix)
    for sid, (label, unit, explain) in FRED_SERIES.items():
        if sid == "CPIYOY":
            cpi = fred_series("CPIAUCSL")
            if cpi.empty or len(cpi) < 13:
                continue
            yoy = (cpi / cpi.shift(12) - 1) * 100
            add(sid, label, unit, explain, yoy.dropna())
        elif sid == "ICSA":
            s = fred_series(sid)
            add(sid, label, unit, explain, (s / 1000).dropna()
                if not s.empty else s)
        else:
            add(sid, label, unit, explain, fred_series(sid))
    return rows


def main():
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    tickers = ([cfg["benchmark"]] + [s["ticker"] for s in cfg["sectors"]]
               + [s["ticker"] for s in cfg["size_style"]])
    prices = {}
    for t in tickers:
        try:
            prices[t] = fetch_history(t)
        except Exception as e:
            print(f"[warn] skipping {t}: {e}")
    try:
        vix = fetch_history("^VIX")
    except Exception:
        vix = pd.Series(dtype=float)
    try:
        tnx = fetch_history("^TNX")
    except Exception:
        tnx = pd.Series(dtype=float)
    oas = fetch_hy_oas()

    # Fail-soft: same contract as every other optional data source here.
    try:
        hmm_regime = regime_hmm.compute_hmm_regime(prices[cfg["benchmark"]],
                                                     vix, oas)
    except Exception as e:
        print(f"[warn] hmm_regime failed: {e}")
        hmm_regime = None

    panel = build_panel(cfg, prices)
    lead = leadership_series(panel, cfg)
    eps = build_episodes(panel, cfg, lead, vix, tnx, oas)
    claims, searched = mine(eps)
    today = panel.index[-1]

    digest = {
        "meta": {
            "generated_utc": datetime.now(timezone.utc)
                .strftime("%Y-%m-%d %H:%M UTC"),
            "proxies": {
                "vix": "stands in for fear/greed sentiment (no free F&G history)",
                "rates": "10-yr yield 63d direction, proxy for rate expectations",
            },
            "caveats": [
                "Leadership is evidence about the regime, not a sector-"
                "picking strategy: mechanical cycle rotation mostly fails "
                "after costs (Stangl-Jacobsen-Visaltanachoti), while "
                "industry momentum persistence is real (Moskowitz-"
                "Grinblatt 1999). Use leadership to condition index-level "
                "decisions, not to chase sectors.",
                "One episode per leadership-regime onset (>=10 days) — "
                "independent-ish, but regimes still overlap macro events.",
                "Sector history is uneven: XLRE 2015+, XLC 2018+, RSP "
                "2003+. Early episodes classify leadership from fewer "
                "groups.",
                "Claims are mined; the searched count is shown. Low "
                "confidence = probably noise.",
            ],
        },
        "current": {
            "as_of": today.strftime("%Y-%m-%d"),
            "leadership": lead.iloc[-1],
            "regimes": {
                "vix": series_regime_vix(vix, today),
                "rates": series_regime_rates(tnx, today),
                "credit": series_regime_credit(oas, today),
            },
        },
        "hmm_regime": hmm_regime,
        "rs_table": rs_table(panel, cfg),
        "relative_curves": relative_curves(panel, cfg),
        "claims": claims[:15],
        "conjunctions_searched": searched,
        "episodes": [e for e in eps][-60:],
        "macro": macro_block(vix),
    }
    os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(digest, f)
    print(f"leadership today: {digest['current']['leadership']} | "
          f"episodes={len(eps)} claims={len(claims)} searched={searched}")


if __name__ == "__main__":
    sys.exit(main())
