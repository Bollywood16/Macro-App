#!/usr/bin/env python3
"""
pit_store.py — Phase C3: the point-in-time Parquet store and its one reader.

Implements exactly what `docs/C3_DESIGN.md` §2/§3 proposed and was approved for
(nothing beyond it — no flows/options data, no C4 backfill logic, no `replay()`,
those are separate phases):

  - Directory layout `data/pit/{table}/{ticker_or_series}/{year}.parquet`
    (`§2.1`).
  - `effective_date` (what the row is about) vs. `processed_date` (when it became
    knowable), defined per table exactly as `§2.2` specifies. For `prices` and
    `macro` (the only two tables this phase populates — VIX and BAA10Y are both
    market-quoted, not survey-revised, so `processed_date == effective_date`
    always, same reasoning as prices' own same-day close) they're identical by
    construction; `flows`/`options` are schema-only per `§2.5`, no writer for them
    yet, and their real lag (when it exists, Phase F) is exactly the case
    `as_of()`'s `processed_date` filter exists to handle correctly.
  - One reader, `as_of(table, key, date) -> pd.DataFrame`: rows whose
    `processed_date <= date`, raises on empty (`§2.1`'s exact contract).
  - `PointInTimeDataContext`, implementing the same `DataContext` protocol
    `data_context.py` already defines for `LiveDataContext`, reading only through
    `as_of()` — never touches yfinance/FRED directly. Every public method ends
    with a shared runtime assertion (`§3.3` point 3, belt-and-suspenders, not a
    substitute for the mutation/canary tests in `scripts/tests/
    test_pit_lookahead_canary.py`) that the returned series' last index is
    `<= as_of_date` — turns a future violation (a new engine wired in without its
    own canary test) into an immediate crash instead of a silent leak.

Adjusted-close (`§4` open question 2): ratified here as an accepted, documented
exception, per the design doc's own recommendation ("standard practice... far
less work" — every quant backtest does this; a 2005 bar's stored value legitimately
depends on a split that hadn't happened yet in 2005, and that's a deterministic
scale factor, not new information about future price direction). Raw/unadjusted
prices + a separate corporate-actions table is not implemented.

Flows/options (`§4` open question 4): the directory shape is reserved (see
FLOWS_DIR/OPTIONS_DIR below) so `as_of()`'s contract doesn't need to change once
Phase F populates them, but nothing writes to them here — matches `§2.5`'s "ship
empty, not stubbed with placeholder data."

What this phase does NOT do (later phases' scope, not shortcuts):
  - `replay(ticker, date)` (C2) — this module gives C2 the context object to call
    into, it doesn't implement replay itself.
  - The full 17-ticker x 2005-2025 backfill as a chunked/resumable GitHub Actions
    workflow with block-count reporting (C4) — see `scripts/pit_seed.py` instead,
    a one-shot local seed script explicitly NOT that workflow (its own docstring
    says so), used here only to give this phase's tests and `as_of()` real data to
    run against.
  - `episodes()` on `PointInTimeDataContext` — the annotated episode library is a
    live Supabase read (`mm_journal("list_episodes", ...)`), not something this
    Parquet store holds; returns `None` unconditionally, same fail-soft posture
    `LiveDataContext.episodes()` already has when no `mm_journal_fn` is injected.
"""
from __future__ import annotations

import os
from datetime import date as date_type, datetime

import pandas as pd
import pyarrow  # noqa: F401  (import-time check: fail loud if missing, not at first write)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_STORE_ROOT = os.path.join(ROOT, "data", "pit")

PRICES_TABLE = "prices"
MACRO_TABLE = "macro"
FLOWS_TABLE = "flows"    # reserved, no writer -- Phase F (§2.5)
OPTIONS_TABLE = "options"  # reserved, no writer -- Phase F (§2.5)

VIX_SERIES = "VIX"
CREDIT_SERIES = "BAA10Y"

# `§2.4`'s C4 requirement, enforced here as a constant both the seed script and
# its own test can check against, so "no later than 1999-01-01" isn't just a
# comment someone has to remember: `credit_regime_series()`'s 1260-trading-day
# (~5yr) percentile window needs this much runway before the replay window's own
# 2005-01-01 start, or 2005-2010 gets `credit_lab='unknown'` for the same
# structural reason the original HY-OAS truncation did.
CREDIT_WARMUP_FLOOR = date_type(1999, 1, 1)


class PITStoreError(Exception):
    """Raised by as_of() on empty results -- distinguishes "no data ingested for
    this key at all" from "this key has data, just none on or before `date`",
    per §2.3's recommendation that as_of() give a diagnosable message rather
    than silently return empty or NaN-filled rows."""


# ------------------------------------------------------------------- layout


def _table_root(table: str, store_root: str = DEFAULT_STORE_ROOT) -> str:
    return os.path.join(store_root, table)


def _key_dir(table: str, key: str, store_root: str = DEFAULT_STORE_ROOT) -> str:
    # Ticker/series names can contain characters that aren't filesystem-safe
    # directory names on every platform (yfinance uses `^SOX`, `^VIX`) -- `^`
    # is fine on Linux/Mac (this app's CI and dev targets) but flagged here
    # rather than silently assumed forever.
    return os.path.join(_table_root(table, store_root), key)


def _year_path(table: str, key: str, year: int, store_root: str = DEFAULT_STORE_ROOT) -> str:
    return os.path.join(_key_dir(table, key, store_root), f"{year}.parquet")


# ------------------------------------------------------------------- writers


def write_prices(ticker: str, ohlcv: pd.DataFrame, store_root: str = DEFAULT_STORE_ROOT) -> int:
    """Writes `ohlcv` (DatetimeIndex; open/high/low/close/volume columns, the
    exact shape `research_engine.fetch_ohlcv()` returns) into
    `{store_root}/prices/{ticker}/{year}.parquet`, one file per calendar year,
    each file fully overwritten from this call's input.

    Full overwrite, not append: `§2.2` ratifies `auto_adjust=True` (retroactive
    adjustment) as an accepted exception, which means a fresh fetch legitimately
    changes what old bars *should* say (a split announced today rescales every
    prior bar) -- there is no meaningful "append-only" story here the way there
    is for `forecasts` ("freeze at creation... corrections are new rows"). A
    price bar isn't a claim frozen at a point in time, it's a slowly-revised
    accounting fact, same as the doc's own framing. Re-running this on a
    schedule with a fresh `fetch_ohlcv()` pull is the intended ingestion
    pattern, not a one-time seed.

    Returns the number of rows written.
    """
    if ohlcv is None or ohlcv.empty:
        raise ValueError(f"write_prices({ticker!r}): refusing to write an empty frame")
    df = ohlcv.copy()
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df = df.sort_index()
    df["effective_date"] = df.index.date
    # prices: processed_date == effective_date always, by design (§2.2) -- a
    # daily close is known same-day after the close, no revision lag modeled.
    df["processed_date"] = df["effective_date"]
    cols = ["effective_date", "processed_date", "open", "high", "low", "close", "volume"]
    df = df[cols]

    key_dir = _key_dir(PRICES_TABLE, ticker, store_root)
    os.makedirs(key_dir, exist_ok=True)
    total = 0
    for year, chunk in df.groupby(df["effective_date"].map(lambda d: d.year)):
        path = _year_path(PRICES_TABLE, ticker, year, store_root)
        chunk.reset_index(drop=True).to_parquet(path, index=False)
        total += len(chunk)
    return total


def write_macro(series_name: str, series: pd.Series, store_root: str = DEFAULT_STORE_ROOT) -> int:
    """Writes `series` (DatetimeIndex, float values -- the shape
    `research_engine.fetch_credit_spread()`/`fetch_history("^VIX")` return) into
    `{store_root}/macro/{series_name}/{year}.parquet`.

    `processed_date == effective_date` for both series this phase populates
    (VIX, BAA10Y) -- both are market-quoted, published same-day, not
    survey/administrative series that get revised after the fact (§2.2's own
    distinction). A future macro series that DOES lag (initial claims, GDP) must
    not reuse this function as-is -- it would need a real publication-date
    source, same warning §2.2 already gives the flows table.
    """
    if series is None or series.empty:
        raise ValueError(f"write_macro({series_name!r}): refusing to write an empty series")
    s = series.copy()
    s.index = pd.to_datetime(s.index).tz_localize(None)
    s = s.sort_index()
    df = pd.DataFrame({"value": s.astype(float)})
    df["effective_date"] = df.index.date
    df["processed_date"] = df["effective_date"]
    df = df[["effective_date", "processed_date", "value"]]

    key_dir = _key_dir(MACRO_TABLE, series_name, store_root)
    os.makedirs(key_dir, exist_ok=True)
    total = 0
    for year, chunk in df.groupby(df["effective_date"].map(lambda d: d.year)):
        path = _year_path(MACRO_TABLE, series_name, year, store_root)
        chunk.reset_index(drop=True).to_parquet(path, index=False)
        total += len(chunk)
    return total


# ------------------------------------------------------------------- reader


def _as_date(d) -> date_type:
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date_type):
        return d
    return pd.to_datetime(d).date()


# Process-lifetime cache of each key's full, unfiltered, concatenated history
# -- keyed by (table, key, store_root). Added after profiling C4's expected
# workload (docs/C3_DESIGN.md §8): a single replay() call was measured at
# ~740ms, ~60% of it (174 separate pd.read_parquet calls) re-reading the same
# ~30-40 year-files from disk that an adjacent call for the same ticker/series
# had just read moments before. A backfill iterating one ticker across ~5,000
# trading dates was about to re-read that ticker's ENTIRE multi-decade history
# from disk 5,000 times to answer 5,000 different truncation questions -- the
# files themselves don't change between those calls, only the `date` filter
# does, so the fix is to cache the read, not the filtered result (the filter
# is cheap; the disk I/O + Arrow->pandas conversion was not).
# NOT invalidated automatically: if something writes new files for a key
# after this process has already read (and cached) it, subsequent as_of()
# calls in THIS process will serve stale data until clear_cache() is called
# explicitly. Fine for a single backfill run or test process (nothing
# reads-then-writes-then-rereads the same key); call clear_cache() first in
# any long-lived process that re-ingests while running.
_read_cache: dict[tuple[str, str, str], pd.DataFrame] = {}


def clear_cache():
    """Drops the in-process read cache `as_of()` keeps per (table, key,
    store_root). Call after writing new data for a key that an earlier
    as_of() call in this same process already read -- see `_read_cache`'s
    own comment for why this isn't automatic."""
    _read_cache.clear()


def _read_all(table: str, key: str, store_root: str) -> pd.DataFrame:
    cache_key = (table, key, store_root)
    if cache_key in _read_cache:
        return _read_cache[cache_key]

    key_dir = _key_dir(table, key, store_root)
    if not os.path.isdir(key_dir):
        raise PITStoreError(
            f"as_of({table!r}, {key!r}, ...): no data ever ingested for this "
            f"key ({key_dir} does not exist)")

    frames = []
    for fname in sorted(os.listdir(key_dir)):
        if fname.endswith(".parquet"):
            frames.append(pd.read_parquet(os.path.join(key_dir, fname)))
    if not frames:
        raise PITStoreError(
            f"as_of({table!r}, {key!r}, ...): key directory exists but holds "
            f"no parquet files ({key_dir})")

    full = pd.concat(frames, ignore_index=True)
    full["effective_date"] = pd.to_datetime(full["effective_date"]).dt.date
    full["processed_date"] = pd.to_datetime(full["processed_date"]).dt.date
    full = full.sort_values("effective_date").reset_index(drop=True)
    _read_cache[cache_key] = full
    return full


def as_of(table: str, key: str, date, store_root: str = DEFAULT_STORE_ROOT) -> pd.DataFrame:
    """Rows whose `processed_date <= date`. Raises `PITStoreError` on empty --
    distinguishing (in the message, for whoever's debugging) "nothing was ever
    ingested for this key" from "this key has data, just none as-of `date`",
    per §2.3.

    Reads every year-partition under the key's directory (not just
    `year <= date.year`) and filters in-memory -- small enough at this phase's
    scale that skipping files by filename-year would only be a premature
    optimization, and it sidesteps ever having to reason about a row whose
    `effective_date` and `processed_date` fall in different calendar years
    (the flows-table lag case §2.2 flags) being filed under the "wrong" year's
    file. The read itself (not the filter) is cached per (table, key,
    store_root) across calls within one process -- see `_read_cache` above.
    """
    d = _as_date(date)
    full = _read_all(table, key, store_root)

    out = full[full["processed_date"] <= d]
    if out.empty:
        first_available = full["effective_date"].min()
        raise PITStoreError(
            f"as_of({table!r}, {key!r}, {d}): key has data, but none on or "
            f"before {d} -- first available effective_date is {first_available}")
    return out.reset_index(drop=True)


# ------------------------------------------------------- PointInTimeDataContext


def _assert_no_lookahead(dates, as_of_date: date_type, label: str):
    """§3.3 point 3: shared belt-and-suspenders check on every public method's
    return value, not a substitute for the canary/mutation tests (point 1) --
    this catches a FUTURE violation (a new call path added later that forgets
    to route through as_of()) as an immediate crash rather than a silent leak
    three phases later."""
    if len(dates) == 0:
        return
    max_date = max(dates)
    assert max_date <= as_of_date, (
        f"PointInTimeDataContext lookahead violation in {label}: returned a row "
        f"dated {max_date}, which is after as_of={as_of_date}")


class PointInTimeDataContext:
    """C3 (`docs/C3_DESIGN.md` §3.1's guarantee, verbatim): for any call through
    `PointInTimeDataContext(as_of=D)`, every row returned has
    `processed_date <= D`, and the returned series' last index is the latest
    available date `<= D`, never later. Implements the same shape as
    `data_context.DataContext` (`close`, `ohlcv`, `vix`, `credit_spread`,
    `episodes`) so C2's `replay()` can hand this to `forecast_engine.run_one()`
    /the tear-sheet engines in place of `LiveDataContext` with no call-site
    changes -- that interchangeability is the entire point of C1's seam.

    Memoized per (method, args) within one instance, same reasoning as
    `LiveDataContext` -- a replay run touching the same ticker/series multiple
    times (e.g. SMH as both the scored ticker and someone else's sector
    analog) shouldn't re-read Parquet repeatedly.
    """

    def __init__(self, as_of_date, store_root: str = DEFAULT_STORE_ROOT,
                 mm_journal_fn=None):
        self.as_of_date = _as_date(as_of_date)
        self.store_root = store_root
        self._mm_journal_fn = mm_journal_fn  # unused today; see episodes() below
        self._close_cache: dict[str, pd.Series] = {}
        self._ohlcv_cache: dict[str, pd.DataFrame] = {}
        self._vix_cache: pd.Series | None = None
        self._credit_spread_cache: pd.Series | None = None

    def _prices_frame(self, ticker: str) -> pd.DataFrame:
        rows = as_of(PRICES_TABLE, ticker, self.as_of_date, self.store_root)
        df = rows.set_index(pd.to_datetime(rows["effective_date"]))
        df.index.name = None
        return df

    def close(self, ticker: str) -> pd.Series:
        if ticker not in self._close_cache:
            df = self._prices_frame(ticker)
            s = df["close"].astype(float)
            _assert_no_lookahead(s.index.date, self.as_of_date, f"close({ticker!r})")
            self._close_cache[ticker] = s
        return self._close_cache[ticker]

    def ohlcv(self, ticker: str) -> pd.DataFrame:
        if ticker not in self._ohlcv_cache:
            df = self._prices_frame(ticker)[["open", "high", "low", "close", "volume"]]
            _assert_no_lookahead(df.index.date, self.as_of_date, f"ohlcv({ticker!r})")
            self._ohlcv_cache[ticker] = df
        return self._ohlcv_cache[ticker]

    def _macro_series(self, series_name: str) -> pd.Series:
        rows = as_of(MACRO_TABLE, series_name, self.as_of_date, self.store_root)
        idx = pd.to_datetime(rows["effective_date"])
        s = pd.Series(rows["value"].astype(float).to_numpy(), index=idx)
        return s

    def vix(self) -> pd.Series:
        if self._vix_cache is None:
            s = self._macro_series(VIX_SERIES)
            _assert_no_lookahead(s.index.date, self.as_of_date, "vix()")
            self._vix_cache = s
        return self._vix_cache

    def credit_spread(self) -> pd.Series:
        if self._credit_spread_cache is None:
            s = self._macro_series(CREDIT_SERIES)
            _assert_no_lookahead(s.index.date, self.as_of_date, "credit_spread()")
            self._credit_spread_cache = s
        return self._credit_spread_cache

    def episodes(self, ticker: str) -> list | None:
        # The annotated episode library is a live Supabase read
        # (mm_journal("list_episodes", ...)), curated prose about ticker
        # history -- not a point-in-time data series this Parquet store models
        # at all, and not something a pre-2026 replay date could honestly
        # source anyway (the annotations describe outcomes that hadn't
        # happened yet on most replay dates). Fails soft to None, same as
        # LiveDataContext.episodes() with no mm_journal_fn injected -- callers
        # already treat this as optional enrichment, never a required input.
        return None


__all__ = [
    "PITStoreError", "as_of", "write_prices", "write_macro",
    "PointInTimeDataContext", "PRICES_TABLE", "MACRO_TABLE", "FLOWS_TABLE",
    "OPTIONS_TABLE", "VIX_SERIES", "CREDIT_SERIES", "CREDIT_WARMUP_FLOOR",
    "DEFAULT_STORE_ROOT",
]
