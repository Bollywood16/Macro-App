"""
Purged walk-forward cross-validation for time-series data with overlapping,
forward-looking labels (Lopez de Prado, "Advances in Financial Machine
Learning", ch. 7). Plain k-fold CV leaks information here because a
training sample's label can be computed from data that falls inside the
test window (or vice versa) whenever a label spans a forward horizon (e.g.
a 5-day-forward-return label).

This is a REUSABLE utility, not tied to any one model. Every fitted
component in this codebase (the HMM regime detector, and the Stage 4 GBT
challenger once it exists) validates itself through
purged_walk_forward_splits() / walk_forward_score() rather than a plain
train_test_split or unpurged KFold — trust machinery built alongside the
fitted component, not bolted on after.

Two defenses, both needed for time-series-with-lookahead-labels:
  PURGE   drop training samples whose label window [i, i+label_horizon)
          overlaps the test fold's index range — prevents training on a
          sample whose label was computed using data inside the test set.
  EMBARGO drop `embargo` additional samples immediately after a test fold
          before training resumes on it — serial correlation (autocorrelated
          returns/vol) means rows right after a test window are still
          informationally close to it even without direct label overlap.

Folds are walk-forward (expanding window): fold k's test set is always
strictly after fold k's training set, matching the point-in-time discipline
used throughout this app (a forecast may only use information available at
its timestamp — MASTER_AGENT_PROMPT.md #4).
"""

from typing import Callable, Iterator, List, Tuple

import numpy as np


def purged_walk_forward_splits(
    n_samples: int,
    n_splits: int,
    label_horizon: int = 0,
    embargo: int = 0,
    min_train: int = 1,
) -> Iterator[Tuple[np.ndarray, np.ndarray]]:
    """Yield (train_idx, test_idx) index arrays for up to `n_splits`
    walk-forward folds over `n_samples` time-ordered rows.

    label_horizon: forward-looking window length (in rows) used to compute
        each sample's label, e.g. a 5-day-forward-return label -> 5. Use 0
        for models with no lookahead label (e.g. an HMM fit directly on
        contemporaneous features).
    embargo: extra rows purged from the START of each fold's training set
        that fall within `embargo` rows after the immediately preceding
        fold's test window (serial-correlation buffer). Has no effect on
        the first fold (nothing precedes it).
    min_train: minimum training-fold size below which a fold is skipped
        (avoids degenerate tiny-train folds at the start of the series).
    """
    if n_splits < 1:
        raise ValueError("n_splits must be >= 1")
    fold_size = n_samples // (n_splits + 1)
    if fold_size < 1:
        raise ValueError("n_samples too small for n_splits")

    prior_test_end = None
    for k in range(1, n_splits + 1):
        test_start = k * fold_size
        if test_start >= n_samples:
            break
        test_end = n_samples if k == n_splits else min(test_start + fold_size,
                                                         n_samples)
        test_idx = np.arange(test_start, test_end)

        # Purge: drop training rows whose forward label window reaches into
        # the test fold.
        train_cutoff = test_start - label_horizon
        train_idx = np.arange(0, max(0, train_cutoff))

        # Embargo: drop rows immediately after the PRECEDING fold's test
        # window (walk-forward training sets are contiguous-from-zero, so
        # embargo only ever needs to trim the boundary from the last fold).
        if embargo > 0 and prior_test_end is not None:
            lo, hi = prior_test_end, prior_test_end + embargo
            train_idx = train_idx[(train_idx < lo) | (train_idx >= hi)]

        prior_test_end = test_end
        if len(train_idx) < min_train:
            continue
        yield train_idx, test_idx


def walk_forward_score(
    fit_fn: Callable,
    score_fn: Callable,
    X: np.ndarray,
    n_splits: int,
    label_horizon: int = 0,
    embargo: int = 0,
    min_train: int = 1,
) -> List[float]:
    """Generic purged walk-forward validation.

    `fit_fn(X_train)` returns a fitted model (any object/tuple your
    `score_fn` understands). `score_fn(fitted, X_test)` returns a float
    (e.g. held-out log-likelihood, or held-out accuracy — higher-is-better
    is the convention callers should use so scores are comparable across
    folds, but this function itself is agnostic).

    Returns the list of per-fold scores. Empty if no fold had enough
    training data (n_samples too small relative to n_splits/min_train).
    """
    n = len(X)
    scores = []
    for train_idx, test_idx in purged_walk_forward_splits(
            n, n_splits, label_horizon=label_horizon, embargo=embargo,
            min_train=min_train):
        model = fit_fn(X[train_idx])
        scores.append(float(score_fn(model, X[test_idx])))
    return scores
