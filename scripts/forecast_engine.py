#!/usr/bin/env python3
"""
Forecast Engine v1 — Market Memory M2.

Produces per-horizon calibrated-ish forecasts for a single ETF at a point in
time, persisted through the mm-journal Edge Function into the immutable
Supabase forecast store (see db/001_market_memory_schema.sql). This is the
quant layer only: it computes probabilities, quantiles, and confidence in
Python. Nothing here is model-vibed, and nothing calls an LLM.

SCOPE (M2, confirmed):
  - Ensemble = analog model + regime-conditioned base rate ONLY. No
    logistic/GBT layer and no news-risk adjustment yet (spec 7.3 items 3/5
    are deferred to a later milestone).
  - Confidence uses a sample-size shrinkage placeholder, not a fitted
    isotonic/Platt calibration (n<30 is too fragile for that to mean
    anything; real calibration is M5, once outcomes exist to calibrate
    against).
  - Universe = scripts/universe_config.json assets UNION scripts/
    rotation_config.json sectors + size_style. The rotation engine already
    proves the data path for the sector/size tickers.

MODELS
  1. Historical analog model (spec 7.4)
     Build a feature vector (trailing returns, RSI, MA distance, drawdown,
     relative-vs-SPY) at the as-of date, z-normalize against the ticker's
     own full history, and find the nearest prior dates in that space.
     Nearby dates are collapsed into independent episodes with a 20-
     trading-day gap so the "same rally" can't be counted twice. v1 is
     within-ticker only — using QQQ's history to inform an SMH analog is
     deferred.
  2. Regime-conditioned base rate (spec 7.3 item 2)
     Same vix/credit/spy_trend regime machinery as research_engine.py,
     vectorized here for performance. Tries the full 3-dim regime match
     first and backs off to 2-dim, 1-dim, then unconditional if there
     aren't enough independent episodes (min 8) at a given depth.
  The two candidate-episode sets are unioned and re-thinned by the same
  20-day gap rule before any statistic is computed, so an episode picked by
  both models is never double-counted.

CONFIDENCE (spec 7.5, shrunk to what v1 can actually support)
  score = 100 * sample_quality * consistency * agreement * analog_density
          - 100 * (data_quality_penalty + distribution_shift_penalty
                    + intraday_proxy_penalty)
  This is a placeholder shrinkage formula, not a fitted model. It is
  intentionally conservative: n=29 with moderate consistency lands in
  "moderate", not "high" (matches the spec 4.3 worked example).

INTRADAY / MANUAL PRICE (spec 5.3)
  An ad hoc run with --price appends a synthetic "today" bar to the
  ticker's close series, recomputes provisional features from it, and
  marks every forecast row intraday_proxy=true with a fixed confidence
  discount — the comparison set (analogs, regime base rate) is still built
  from end-of-day history, so provisional intraday features are being
  compared against a daily-trained distribution.

Output: nothing is committed to data/*.json. Forecasts belong in Supabase
(the architecture decision in MASTER_AGENT_PROMPT.md section 2), reached
only through the passphrase-gated mm-journal Edge Function. Set
APP_PASSPHRASE to enable persistence; without it the script computes and
prints everything but skips the write (fail-soft, so this never breaks CI
before the secret exists).
"""

import argparse
import json
import math
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, time as dtime, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import research_engine as re_engine  # noqa: E402  (reuse fetch/rsi/HY-OAS)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
UNIVERSE_PATH = os.path.join(HERE, "universe_config.json")
ROTATION_CONFIG_PATH = os.path.join(HERE, "rotation_config.json")

FUNCTION_URL = os.environ.get(
    "MM_FUNCTION_URL",
    "https://anzbpxqvibgpxnwgyqoc.supabase.co/functions/v1/mm-journal",
)

HORIZONS = [1, 5, 20, 60]
BENCHMARK = "SPY"
EPISODE_GAP = 20            # trading days; spec 7.4 item 5's example gap
ANALOG_CANDIDATES = 120     # nearest neighbors considered before thinning
ANALOG_KEEP = 60            # cap on independent analog episodes kept
REGIME_COOLDOWN = EPISODE_GAP
MIN_REGIME_N = 8
MIN_EPISODES_FOR_SIGNAL = 8
INTRADAY_CONFIDENCE_DISCOUNT = 0.15
MODEL_VERSION = "mm-forecast-v1.0-analog+regime-baserate"

FEATURE_FIELDS = [
    "ret_1d", "ret_5d", "ret_20d", "ret_60d", "ret_120d", "rsi14",
    "dist_ma50", "dist_ma200", "drawdown_252", "rel_spy_63d", "vol_21d",
    "mom_12m",
]
TRADING_DAYS_YEAR = 252

LABEL_TEXT = {
    "no_reliable_signal":
        "No reliable signal — weak data, small sample, or model "
        "disagreement. Stand down.",
    "hold_no_change":
        "Insufficient edge to alter existing exposure. Log a hold if "
        "useful.",
    "watch_for_confirmation":
        "Setup forming but the gate is not passed. No position "
        "recommendation yet — create a watch item.",
    "reduce_defensive_candidate":
        "Downside probability or expected loss is elevated. Review "
        "sizing; treat as a risk warning, not an instruction.",
    "moderate_long_candidate":
        "Moderate-conviction tactical long candidate. Consider staged "
        "entry; do not treat as certainty.",
    "high_conviction_long_candidate":
        "High-conviction candidate: rare and evidence-rich setup. Review "
        "and log a decision.",
}

# ------------------------------------------------------------- universe


def load_universe():
    with open(UNIVERSE_PATH) as f:
        research_assets = json.load(f)["assets"]
    with open(ROTATION_CONFIG_PATH) as f:
        rot = json.load(f)
    merged, seen = [], set()
    for a in research_assets + rot["sectors"] + rot["size_style"]:
        t = a["ticker"]
        if t in seen:
            continue
        seen.add(t)
        merged.append({"ticker": t, "label": a["label"]})
    return merged


# --------------------------------------------------------- feature build


def append_manual_price(close: pd.Series, price: float) -> pd.Series:
    today = pd.Timestamp.now().normalize()
    s = close.copy()
    if s.index[-1].normalize() == today:
        s.iloc[-1] = price
    else:
        s = pd.concat([s, pd.Series([price], index=[today])])
    return s


def build_feature_frame(close: pd.Series, spy_close: pd.Series) -> pd.DataFrame:
    df = pd.DataFrame({"close": close})
    df["ma50"] = close.rolling(50).mean()
    df["ma200"] = close.rolling(200).mean()
    df["rsi14"] = re_engine.rsi(close)
    df["ret_1d"] = close.pct_change(1)
    df["ret_5d"] = close.pct_change(5)
    df["ret_20d"] = close.pct_change(20)
    df["ret_60d"] = close.pct_change(60)
    df["ret_120d"] = close.pct_change(120)
    df["dist_ma50"] = close / df["ma50"] - 1
    df["dist_ma200"] = close / df["ma200"] - 1
    df["hi252"] = close.rolling(252).max()
    df["drawdown_252"] = close / df["hi252"] - 1
    spy_aligned = spy_close.reindex(close.index).ffill()
    df["rel_spy_63d"] = close.pct_change(63) - spy_aligned.pct_change(63)
    # Moreira-Muir: realized vol is far more persistent/predictable than
    # expected return, so a 21d trailing realized-vol estimate is a
    # reasonable proxy for near-term risk. Annualized (sqrt(252)) so it's
    # comparable across horizons via sqrt-time scaling at the call site.
    df["vol_21d"] = close.pct_change().rolling(21).std() * math.sqrt(TRADING_DAYS_YEAR)
    # Moskowitz-Ooi-Pedersen time-series momentum: trailing 12-month return.
    # TSMOM sizes by the SIGN of an asset's own past return, not its return
    # relative to other assets (that's cross-sectional momentum, a different
    # effect, not implemented here). The paper uses return in excess of the
    # T-bill rate; we don't carry a risk-free series, and at ~12m horizons
    # the T-bill rate is a small, roughly constant offset relative to typical
    # equity/ETF return dispersion, so raw trailing return is used as an
    # explicit simplification of "excess" (documented here, not silent).
    df["mom_12m"] = close.pct_change(252)
    return df


def spy_trend_frame(spy_close: pd.Series) -> pd.DataFrame:
    df = pd.DataFrame({"close": spy_close})
    df["ma200"] = spy_close.rolling(200).mean()
    return df


# ------------------------------------------------------- regime machinery


def regime_series(idx, vix: pd.Series, oas: pd.Series, spy_trend_df: pd.DataFrame):
    vix_al = vix.reindex(idx).ffill()
    vix_lab = np.where(vix_al.isna(), "unknown",
                        np.where(vix_al < 20, "calm",
                                 np.where(vix_al <= 30, "elevated", "stressed")))

    oas_al = oas.reindex(idx).ffill()
    chg = oas_al - oas_al.shift(EPISODE_GAP * 3)  # ~63d change, FRED HY OAS
    credit_lab = np.where(chg.isna(), "unknown",
                           np.where(chg > 0.25, "widening",
                                    np.where(chg < -0.25, "narrowing", "flat")))

    spy_close_al = spy_trend_df["close"].reindex(idx).ffill()
    spy_ma200_al = spy_trend_df["ma200"].reindex(idx).ffill()
    spy_lab = np.where(spy_ma200_al.isna(), "unknown",
                        np.where(spy_close_al >= spy_ma200_al, "above", "below"))

    return list(zip(vix_lab.tolist(), credit_lab.tolist(), spy_lab.tolist()))


def thin_sequential(positions, gap):
    out, last = [], -10 ** 9
    for i in positions:
        if i - last >= gap:
            out.append(i)
            last = i
    return out


def thin_by_distance(positions_by_distance, gap, keep_max):
    kept = []
    for pos in positions_by_distance:
        if all(abs(pos - k) >= gap for k in kept):
            kept.append(pos)
        if len(kept) >= keep_max:
            break
    return kept


def regime_conditioned_positions(regime_tuples, current_tuple, gap, min_n):
    for depth in (3, 2, 1, 0):
        if depth == 0:
            positions = list(range(len(regime_tuples)))
        else:
            positions = [i for i, t in enumerate(regime_tuples)
                         if t[:depth] == current_tuple[:depth]]
        thinned = thin_sequential(positions, gap)
        if len(thinned) >= min_n or depth == 0:
            return thinned, depth
    return [], 0


# --------------------------------------------------------------- analog


def normalize_matrix(df: pd.DataFrame, fields):
    X = df[fields].to_numpy(dtype=float)
    stats = {}
    for j, f in enumerate(fields):
        col = X[:, j]
        mu = np.nanmean(col)
        sd = np.nanstd(col)
        if not sd or math.isnan(sd) or sd == 0:
            sd = 1.0
        if math.isnan(mu):
            mu = 0.0
        stats[f] = {"mean": float(mu), "std": float(sd)}
        X[:, j] = (col - mu) / sd
    return X, stats


def analog_positions(X: np.ndarray, query_pos: int):
    n = X.shape[0]
    valid = ~np.isnan(X).any(axis=1)
    valid[query_pos] = False
    if not valid.any():
        return [], None
    diffs = X[valid] - X[query_pos]
    dists = np.sqrt(np.sum(diffs ** 2, axis=1))
    valid_positions = np.where(valid)[0]
    order = np.argsort(dists)
    sorted_positions = valid_positions[order].tolist()
    nearest_dist = float(dists[order[0]]) if len(order) else None
    kept = thin_by_distance(sorted_positions[:ANALOG_CANDIDATES], EPISODE_GAP,
                             ANALOG_KEEP)
    return kept, nearest_dist


# ------------------------------------------------------------ horizon stats


def horizon_stats(close: pd.Series, spy_close_aligned: pd.Series, positions,
                   horizon):
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


# --------------------------------------------------------- confidence etc.


def compute_confidence(n, p_positive, analog_p, regime_p, analog_density,
                        distribution_shift, intraday_proxy, regime_unknown):
    sample_quality = min(1.0, n / 40.0)
    consistency = max(p_positive, 1 - p_positive)
    if analog_p is not None and regime_p is not None:
        agreement = 1 - abs(analog_p - regime_p)
    else:
        agreement = 0.6
    data_quality_penalty = 0.1 if regime_unknown else 0.0
    distribution_shift_penalty = 0.15 if distribution_shift else 0.0
    intraday_penalty = INTRADAY_CONFIDENCE_DISCOUNT if intraday_proxy else 0.0

    score = (100 * sample_quality * consistency * agreement * analog_density
              - 100 * (data_quality_penalty + distribution_shift_penalty
                        + intraday_penalty))
    score = max(0, min(100, round(score)))
    label = "high" if score >= 70 else ("moderate" if score >= 40 else "low")
    return score / 100.0, label


def recommendation_label(n, confidence_label, p_positive, expected_mae):
    if n < MIN_EPISODES_FOR_SIGNAL or confidence_label == "low":
        return "no_reliable_signal"
    edge = p_positive - 0.5
    if abs(edge) < 0.05:
        return "hold_no_change"
    if edge < 0:
        if p_positive <= 0.4 or (expected_mae is not None and expected_mae < -0.08):
            return "reduce_defensive_candidate"
        return "watch_for_confirmation"
    if (confidence_label == "high" and p_positive >= 0.62
            and (expected_mae is None or expected_mae > -0.08)):
        return "high_conviction_long_candidate"
    if p_positive >= 0.55:
        return "moderate_long_candidate"
    return "watch_for_confirmation"


def build_drivers(ticker, query, regimes, intraday_proxy):
    b = []
    if pd.notna(query.get("ret_1d")):
        b.append(f"1-day return is {query['ret_1d'] * 100:+.1f}%.")
    if pd.notna(query.get("rsi14")):
        tag = " (provisional intraday)" if intraday_proxy else ""
        b.append(f"RSI-14 is {query['rsi14']:.1f}{tag}.")
    if pd.notna(query.get("dist_ma200")):
        pos = "above" if query["dist_ma200"] >= 0 else "below"
        b.append(f"{ticker} is {pos} its 200-day average by "
                  f"{query['dist_ma200'] * 100:+.1f}%.")
    if pd.notna(query.get("vol_21d")):
        b.append(f"21-day realized volatility (annualized) is "
                  f"{query['vol_21d'] * 100:.1f}%.")
    if pd.notna(query.get("mom_12m")):
        d = "positive" if query["mom_12m"] >= 0 else "negative"
        b.append(f"Trailing 12-month time-series momentum is {d} "
                  f"({query['mom_12m'] * 100:+.1f}%).")
    b.append(f"Credit spreads (HY OAS, 63d) are {regimes[1]}.")
    if ticker != BENCHMARK and pd.notna(query.get("rel_spy_63d")):
        d = "outperforming" if query["rel_spy_63d"] >= 0 else "underperforming"
        b.append(f"{ticker} is {d} SPY by "
                  f"{abs(query['rel_spy_63d']) * 100:.1f} percentage points "
                  "over the trailing quarter.")
    return b


def build_invalidation_risks(ticker, regimes, distribution_shift):
    risks = [
        f"Regime shift: this setup is conditioned on {regimes[0]} VIX / "
        f"{regimes[1]} credit / SPY-{regimes[2]}-200dma; a shift in any of "
        "these invalidates the conditioning.",
        "Historical analogs cluster in a small number of episodes/decades; "
        "a genuinely novel macro event would not be represented.",
        "News/event risk (earnings, policy, supply chain) is not modeled "
        "in this milestone (M2) — binary event risk is unrepresented.",
    ]
    if distribution_shift:
        risks.append(
            f"{ticker}'s current feature vector is unusually far from its "
            "own historical distribution (out-of-distribution warning) — "
            "analog matches are weaker than usual.")
    return risks


# ------------------------------------------------------------- mm-journal


def mm_journal(op, payload):
    passphrase = os.environ.get("APP_PASSPHRASE")
    if not passphrase:
        print(f"[warn] APP_PASSPHRASE not set — skipping persistence for {op}")
        return None
    body = json.dumps({"op": op, "payload": payload}).encode()
    req = urllib.request.Request(
        FUNCTION_URL, data=body, method="POST",
        headers={"Content-Type": "application/json", "x-app-key": passphrase},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        print(f"[warn] mm-journal {op} failed: HTTP {e.code} {detail}")
        return None
    except Exception as e:
        print(f"[warn] mm-journal {op} failed: {e}")
        return None


# ---------------------------------------------------------------- run one


def run_one(asset, universe_prices, spy_close, spy_trend_df, vix, oas,
            manual_price, market_status, dry_run):
    ticker = asset["ticker"]
    close = universe_prices.get(ticker)
    if close is None or len(close) < 260:
        print(f"[warn] skipping {ticker}: insufficient history")
        return None

    intraday_proxy = manual_price is not None
    confidence_discount = INTRADAY_CONFIDENCE_DISCOUNT if intraday_proxy else 0.0

    if intraday_proxy:
        close = append_manual_price(close, manual_price)

    if ticker == BENCHMARK and intraday_proxy:
        spy_close_run = close
        spy_trend_run = spy_trend_frame(spy_close_run)
    else:
        spy_close_run = spy_close
        spy_trend_run = spy_trend_df

    df = build_feature_frame(close, spy_close_run)
    query_pos = len(df) - 1
    query = df.iloc[query_pos]
    effective_price = float(query["close"])

    regime_tuples = regime_series(df.index, vix, oas, spy_trend_run)
    current_regime = regime_tuples[query_pos]
    regime_unknown = "unknown" in current_regime

    X, norm_stats = normalize_matrix(df, FEATURE_FIELDS)
    analog_pos, nearest_dist = analog_positions(X, query_pos)
    ood_threshold = math.sqrt(len(FEATURE_FIELDS))
    distribution_shift = bool(nearest_dist is not None
                               and nearest_dist > ood_threshold)
    analog_density = max(0.0, min(1.0, 1 - (nearest_dist or 0) /
                                   (2 * ood_threshold)))

    regime_pos, regime_depth = regime_conditioned_positions(
        regime_tuples[:query_pos], current_regime, REGIME_COOLDOWN, MIN_REGIME_N)

    ensemble_pos = list(analog_pos)
    for pos in regime_pos:
        if all(abs(pos - k) >= EPISODE_GAP for k in ensemble_pos):
            ensemble_pos.append(pos)
    ensemble_pos.sort()

    as_of = datetime.now(timezone.utc)
    if intraday_proxy:
        provider_ts = None
        retrieved_ts = as_of.isoformat()
        is_indicative = True
        source = "manual"
    else:
        last_date = close.index[-1]
        close_dt = datetime.combine(last_date.date(), dtime(16, 0),
                                     tzinfo=ZoneInfo("America/New_York"))
        provider_ts = close_dt.astimezone(timezone.utc).isoformat()
        retrieved_ts = as_of.isoformat()
        is_indicative = False
        source = "provider"

    snap_payload = {
        "ticker": ticker, "price": round(effective_price, 6), "source": source,
        "provider_ts": provider_ts, "retrieved_ts": retrieved_ts,
        "market_status": market_status, "is_indicative": is_indicative,
    }
    snap_resp = None if dry_run else mm_journal("create_quote_snapshot", snap_payload)
    quote_snapshot_id = (snap_resp or {}).get("quote_snapshot", {}).get("id")

    horizon_rows = {}
    for h in HORIZONS:
        ens = horizon_stats(close, spy_close_run, ensemble_pos, h)
        analog_h = horizon_stats(close, spy_close_run, analog_pos, h)
        regime_h = horizon_stats(close, spy_close_run, regime_pos, h)
        horizon_rows[h] = {"ensemble": ens, "analog": analog_h, "regime": regime_h}

    primary = horizon_rows.get(5) or horizon_rows.get(HORIZONS[0])
    ens5 = primary["ensemble"]
    if ens5:
        conf_score, conf_label = compute_confidence(
            ens5["n"], ens5["p_positive"],
            (primary["analog"] or {}).get("p_positive"),
            (primary["regime"] or {}).get("p_positive"),
            analog_density, distribution_shift, intraday_proxy, regime_unknown)
        rec_label = recommendation_label(ens5["n"], conf_label,
                                          ens5["p_positive"], ens5["expected_mae"])
    else:
        conf_score, conf_label, rec_label = 0.0, "low", "no_reliable_signal"

    drivers = build_drivers(ticker, query, current_regime, intraday_proxy)
    invalidation_risks = build_invalidation_risks(ticker, current_regime,
                                                   distribution_shift)
    warnings = []
    if intraday_proxy:
        warnings.append("Intraday proxy uses a daily-trained model; "
                         "confidence is discounted.")
    if regime_unknown:
        warnings.append("One or more regime dimensions are unknown "
                         "(insufficient VIX/credit/SPY history at this date).")
    if distribution_shift:
        warnings.append("Current feature vector is out-of-distribution "
                         "versus this ticker's own history.")

    features_json = {
        "as_of": as_of.isoformat(),
        "query_features": {f: (None if pd.isna(query[f]) else round(float(query[f]), 6))
                            for f in FEATURE_FIELDS},
        "normalization": norm_stats,
        "regime": {"vix": current_regime[0], "credit": current_regime[1],
                   "spy_trend": current_regime[2], "match_depth": regime_depth},
        "intraday_proxy": intraday_proxy,
        "confidence_discount": confidence_discount,
        "nearest_analog_distance": nearest_dist,
        "analog_density": round(analog_density, 4),
        "distribution_shift": distribution_shift,
    }

    vol_21d_now = query.get("vol_21d")
    has_vol = pd.notna(vol_21d_now) and vol_21d_now > 0

    created_forecast_ids = []
    for h in HORIZONS:
        ens = horizon_rows[h]["ensemble"]
        if ens:
            conf_h, conf_label_h = compute_confidence(
                ens["n"], ens["p_positive"],
                (horizon_rows[h]["analog"] or {}).get("p_positive"),
                (horizon_rows[h]["regime"] or {}).get("p_positive"),
                analog_density, distribution_shift, intraday_proxy, regime_unknown)
        else:
            conf_h, conf_label_h = 0.0, "low"

        # Moreira-Muir vol-managed signal: scale the horizon's median expected
        # return by realized vol scaled (sqrt-time) to that same horizon, so
        # horizons are comparable as a reward-per-unit-of-risk figure. A big
        # edge during an elevated-vol stretch nets a smaller ratio here than
        # the same edge during a calm stretch — vol itself is the
        # predictable/actionable part per Moreira-Muir, expected return isn't.
        return_per_unit_vol = None
        if ens and has_vol:
            sigma_h = float(vol_21d_now) * math.sqrt(h / TRADING_DAYS_YEAR)
            if sigma_h > 0:
                return_per_unit_vol = round(ens["q50"] / sigma_h, 4)

        evidence_json = {
            "recommendation_label": rec_label,
            "recommendation_basis_horizon_days": 5,
            "model_action": LABEL_TEXT[rec_label],
            "why_it_triggered": drivers,
            "invalidation_risks": invalidation_risks,
            "warnings": warnings,
            "analog_episode_count": len(analog_pos),
            "regime_episode_count": len(regime_pos),
            "regime_match_depth": regime_depth,
            "ensemble_mean_excess_return": ens.get("mean_excess_return") if ens else None,
            "vol_management": {
                "realized_vol_21d_annualized": (
                    round(float(vol_21d_now), 4) if has_vol else None),
                "horizon_days": h,
                "expected_return_per_unit_vol": return_per_unit_vol,
            },
            "sub_models": {
                "analog": horizon_rows[h]["analog"],
                "regime_base_rate": horizon_rows[h]["regime"],
            },
        }

        payload = {
            "ticker": ticker, "as_of_ts": as_of.isoformat(),
            "effective_price": round(effective_price, 6),
            "quote_snapshot_id": quote_snapshot_id,
            "horizon_days": h, "benchmark": BENCHMARK,
            "p_positive": ens["p_positive"] if ens else None,
            "p_beat_benchmark": ens["p_beat_benchmark"] if ens else None,
            "q20": ens["q20"] if ens else None,
            "q50": ens["q50"] if ens else None,
            "q80": ens["q80"] if ens else None,
            "expected_mae": ens["expected_mae"] if ens else None,
            "n_independent": ens["n"] if ens else 0,
            "confidence_score": conf_h, "confidence_label": conf_label_h,
            "model_version": MODEL_VERSION,
            "features_json": features_json, "evidence_json": evidence_json,
        }
        if dry_run or not quote_snapshot_id:
            created_forecast_ids.append(None)
        else:
            resp = mm_journal("create_forecast", payload)
            created_forecast_ids.append((resp or {}).get("forecast", {}).get("id"))

    return {
        "ticker": ticker, "effective_price": effective_price,
        "intraday_proxy": intraday_proxy, "recommendation_label": rec_label,
        "confidence_score": conf_score, "confidence_label": conf_label,
        "primary_horizon": primary, "quote_snapshot_id": quote_snapshot_id,
        "forecast_ids": created_forecast_ids, "horizon_rows": horizon_rows,
        "drivers": drivers, "invalidation_risks": invalidation_risks,
        "warnings": warnings, "regime": current_regime,
        "vol_21d": round(float(vol_21d_now), 4) if has_vol else None,
    }


def print_recommendation_card(result):
    if not result:
        return
    ens = result["primary_horizon"]["ensemble"]
    print(f"\n{result['ticker']} | {result['recommendation_label']} | "
          f"5 trading days")
    print(f"Effective analysis price: {result['effective_price']:.2f}"
          f"{' (manual override)' if result['intraday_proxy'] else ''}")
    if ens:
        print(f"Probability positive: {ens['p_positive'] * 100:.0f}%")
        if ens["p_beat_benchmark"] is not None:
            print(f"Probability outperforming SPY: "
                  f"{ens['p_beat_benchmark'] * 100:.0f}%")
        print(f"Median expected return: {ens['q50'] * 100:+.1f}%")
        print(f"20th-80th percentile: {ens['q20'] * 100:+.1f}% to "
              f"{ens['q80'] * 100:+.1f}%")
        if result.get("vol_21d") is not None:
            sigma_5d = result["vol_21d"] * math.sqrt(5 / TRADING_DAYS_YEAR)
            rpuv = ens["q50"] / sigma_5d if sigma_5d > 0 else None
            print(f"21d realized vol (annualized): {result['vol_21d'] * 100:.1f}%"
                  + (f" | return per unit vol (5d): {rpuv:+.2f}"
                     if rpuv is not None else ""))
        print(f"Expected maximum adverse excursion: "
              f"{ens['expected_mae'] * 100:+.1f}%")
        print(f"Independent historical episodes: {ens['n']}")
    print(f"Confidence: {result['confidence_label']} "
          f"({result['confidence_score']:.2f})")
    print("Why it triggered:")
    for d in result["drivers"]:
        print(f"  - {d}")
    print(f"Regime: VIX {result['regime'][0]} | credit {result['regime'][1]} "
          f"| SPY-trend {result['regime'][2]}")
    print("Invalidation risks:")
    for r in result["invalidation_risks"]:
        print(f"  - {r}")
    if result["warnings"]:
        print("Warnings:")
        for w in result["warnings"]:
            print(f"  - {w}")
    print(f"Model action: {LABEL_TEXT[result['recommendation_label']]}")
    print(f"model_version: {MODEL_VERSION}")


# -------------------------------------------------------------------- main


def main():
    ap = argparse.ArgumentParser(description="Market Memory forecast engine")
    ap.add_argument("--ticker", help="Single ticker for an on-demand run "
                     "(default: batch over the full universe)")
    ap.add_argument("--price", type=float, help="Manual effective price "
                     "(marks intraday_proxy=true and discounts confidence)")
    ap.add_argument("--market-status", default="unknown",
                     choices=["open", "closed", "pre", "post", "unknown"])
    ap.add_argument("--dry-run", action="store_true",
                     help="Compute and print only; skip mm-journal writes")
    args = ap.parse_args()

    universe = load_universe()
    try:
        spy_close = re_engine.fetch_history("SPY")
    except Exception as e:
        print(f"[error] cannot fetch SPY, aborting: {e}")
        return 1
    spy_trend_df = spy_trend_frame(spy_close)
    try:
        vix = re_engine.fetch_history("^VIX")
    except Exception:
        vix = pd.Series(dtype=float)
    oas = re_engine.fetch_hy_oas()

    tickers = [args.ticker.upper()] if args.ticker else [a["ticker"] for a in universe]
    label_by_ticker = {a["ticker"]: a["label"] for a in universe}

    universe_prices = {"SPY": spy_close}
    for t in tickers:
        if t in universe_prices:
            continue
        try:
            universe_prices[t] = re_engine.fetch_history(t)
        except Exception as e:
            print(f"[warn] skipping {t}: {e}")

    on_demand = args.ticker is not None
    for t in tickers:
        asset = {"ticker": t, "label": label_by_ticker.get(t, t)}
        market_status = "closed" if not on_demand else args.market_status
        result = run_one(asset, universe_prices, spy_close, spy_trend_df,
                          vix, oas, args.price if on_demand else None,
                          market_status, args.dry_run)
        if on_demand:
            if not result:
                # A silent no-op run shows green in Actions and leaves the
                # app UI stuck polling until it times out at 90s with no
                # explanation. Fail loudly instead — an on-demand ticker
                # with no usable yfinance history (delisted, too new, or a
                # typo) is a clean, diagnosable error, not "still running".
                print(f"[error] no forecast produced for {t}: yfinance has "
                      f"no usable price history (need 260+ trading days).")
                return 1
            print_recommendation_card(result)
        elif result:
            ens = result["primary_horizon"]["ensemble"]
            print(f"{t}: {result['recommendation_label']} "
                  f"conf={result['confidence_label']} "
                  f"n={ens['n'] if ens else 0}")


if __name__ == "__main__":
    sys.exit(main())
