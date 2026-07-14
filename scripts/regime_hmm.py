"""
Data-driven regime detector: a 2-3 state Gaussian HMM fit on daily SPY
return, realized volatility, and credit-spread direction.

This is explicitly a COMPARISON signal, not a replacement. Every other
engine in this app (forecast_engine.py, research_engine.py,
rotation_engine.py) conditions on hand-tagged regime rules (VIX thresholds,
HY-OAS 63d change, price-vs-200dma) because those rules are simple, auditable,
and point-in-time-safe by construction. An HMM's states are unsupervised and
can drift or relabel across refits; it is surfaced ALONGSIDE the hand tags
so a fitted model's regime call can be sanity-checked against them, and it
does not feed into any existing engine's conditioning in this milestone.

Fails soft (returns None) if hmmlearn isn't installed or there isn't enough
history to fit meaningfully — same fail-soft contract as the GDELT/FRED
adapters elsewhere in scripts/.
"""

import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from purged_cv import walk_forward_score  # noqa: E402

N_STATES = 3
MIN_HISTORY_ROWS = 500       # ~2 years of daily data; below this an HMM fit
                              # is more noise than signal
RANDOM_STATE = 42            # HMM fitting (EM) is stochastic; fix the seed
                              # so a re-run on the same data reproduces the
                              # same state assignment (point-in-time
                              # discipline: a digest shouldn't flip on
                              # re-run for no data reason)

STATE_NAMES = {2: ["risk_on", "risk_off"],
               3: ["risk_on", "choppy", "risk_off"]}


def build_hmm_features(spy_close: pd.Series, vix: pd.Series,
                        oas: pd.Series) -> pd.DataFrame:
    """Daily feature frame: SPY 1d return, 21d realized vol (annualized),
    63d HY-OAS change (credit-spread direction, points). VIX itself is left
    out deliberately — realized vol from SPY's own returns and VIX are
    highly collinear, and the hand-tagged regime already uses VIX directly,
    so including both would just let the HMM re-derive the VIX rule instead
    of adding new information."""
    df = pd.DataFrame(index=spy_close.index)
    df["ret_1d"] = spy_close.pct_change()
    df["vol_21d"] = df["ret_1d"].rolling(21).std() * np.sqrt(252)
    oas_al = oas.reindex(spy_close.index).ffill()
    df["credit_chg_63d"] = oas_al - oas_al.shift(63)
    return df.dropna()


def _standardize(X: np.ndarray):
    mu, sd = X.mean(axis=0), X.std(axis=0)
    sd = np.where(sd == 0, 1.0, sd)
    return (X - mu) / sd, mu, sd


def fit_hmm(X: np.ndarray, n_states: int = N_STATES):
    from hmmlearn.hmm import GaussianHMM
    Xz, mu, sd = _standardize(X)
    model = GaussianHMM(n_components=n_states, covariance_type="diag",
                         n_iter=200, random_state=RANDOM_STATE)
    model.fit(Xz)
    return model, mu, sd


def _label_states(model, mu, sd, n_states: int):
    """hmmlearn assigns state indices in fit order (arbitrary). Rank states
    by their fitted mean daily return (unstandardized, dimension 0 =
    ret_1d) so labels are meaningful across refits instead of being
    arbitrary integers."""
    means = model.means_[:, 0] * sd[0] + mu[0]
    order = np.argsort(-means)  # best average return first
    names = STATE_NAMES.get(n_states, [f"state_{i}" for i in range(n_states)])
    return {int(order[i]): names[i] for i in range(n_states)}


def hmm_walkforward_diagnostic(X: np.ndarray, n_states: int = N_STATES,
                                n_splits: int = 3, embargo: int = 21,
                                min_train: int = 300):
    """Purged walk-forward out-of-sample average log-likelihood per row —
    a goodness-of-fit / overfitting check for the HMM, per the directive
    that trust machinery (purged CV) is built alongside any fitted
    component, HMM included. Diagnostic only: it does not select n_states
    or feed back into any engine in this milestone.

    embargo=21 (~1 trading month) purges training rows right after each
    test fold, since realized vol / credit-spread-change are themselves
    rolling-window features and are still informationally close to the
    test window just past their own window length.

    n_splits/min_train default lower than MIN_HISTORY_ROWS (the main-fit
    floor) on purpose: the credit-spread series is the shortest input this
    app fetches anywhere (HY OAS via FRED), so the diagnostic degrades to
    fewer, still-honest folds rather than silently producing zero folds
    whenever the shared history is on the shorter side.
    """
    def fit_fn(X_train):
        return fit_hmm(X_train, n_states)

    def score_fn(fitted, X_test):
        model, mu, sd = fitted
        Xz_test = (X_test - mu) / sd
        return model.score(Xz_test) / len(X_test)  # avg log-lik per row

    scores = walk_forward_score(fit_fn, score_fn, X, n_splits=n_splits,
                                 label_horizon=0, embargo=embargo,
                                 min_train=min_train)
    if not scores:
        return None
    return {
        "n_folds": len(scores),
        "mean_oos_loglik_per_row": round(float(np.mean(scores)), 4),
        "fold_scores": [round(s, 4) for s in scores],
    }


def compute_hmm_regime(spy_close: pd.Series, vix: pd.Series, oas: pd.Series,
                        n_states: int = N_STATES,
                        include_diagnostic: bool = True):
    """Returns a digest-ready dict, or None if hmmlearn is unavailable or
    there isn't enough history — fails soft, never raises, matching every
    other optional data source in scripts/."""
    try:
        feats = build_hmm_features(spy_close, vix, oas)
    except Exception as e:
        print(f"[warn] hmm_regime: feature build failed: {e}")
        return None
    if len(feats) < MIN_HISTORY_ROWS:
        print(f"[warn] hmm_regime: only {len(feats)} rows of history, "
              f"need {MIN_HISTORY_ROWS} — skipping")
        return None

    X = feats.to_numpy()
    try:
        model, mu, sd = fit_hmm(X, n_states)
    except ImportError:
        print("[warn] hmm_regime: hmmlearn not installed — skipping")
        return None
    except Exception as e:
        print(f"[warn] hmm_regime: fit failed: {e}")
        return None

    label_by_state = _label_states(model, mu, sd, n_states)
    Xz, _, _ = _standardize(X)
    state_seq = model.predict(Xz)
    probs = model.predict_proba(Xz)
    current_state = int(state_seq[-1])
    current_probs = probs[-1]

    out = {
        "as_of": feats.index[-1].strftime("%Y-%m-%d"),
        "n_states": n_states,
        "state_labels": [label_by_state[s] for s in range(n_states)],
        "current": {
            "state_label": label_by_state[current_state],
            "probabilities": {label_by_state[s]: round(float(current_probs[s]), 4)
                               for s in range(n_states)},
        },
        "note": ("Data-driven comparison to the hand-tagged VIX/credit/"
                 "SPY-trend regime labels elsewhere in this app — not a "
                 "replacement, and it does not feed their conditioning. Fit "
                 "via a 2-3 state Gaussian HMM on SPY daily return, 21d "
                 "realized vol, and 63d HY-OAS change; states are "
                 "unsupervised and labeled post-hoc by fitted mean-return "
                 "rank, not hand-defined thresholds."),
    }
    if include_diagnostic:
        out["walkforward_diagnostic"] = hmm_walkforward_diagnostic(X, n_states)
    return out
