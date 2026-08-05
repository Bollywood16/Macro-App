#!/usr/bin/env python3
"""
data_context.py — Phase C1: the seam between "compute" and "fetch."

DataContext is the interface every data-dependent call in forecast_engine.py
(and, through it, every engine in engines/) goes through instead of calling
re_engine.fetch_* or mm_journal("list_episodes", ...) directly. Two
implementations:

  LiveDataContext         wraps research_engine's yfinance/FRED fetches and
                           an injected mm_journal callable, memoized per
                           ticker/call so a run only ever fetches each thing
                           once. This is a pure refactor of today's live
                           path -- same data, same source, just fetched
                           through one seam instead of scattered inline
                           calls. Behavior must be byte-identical to the
                           pre-refactor code (verified separately, see
                           scripts/tests/test_data_context_parity.py).

  PointInTimeDataContext   Phase C3. Not implemented here yet -- reads the
                           Parquet store under data/pit/ via as_of(table,
                           ticker, date), using only data published on or
                           before that date. This is what makes
                           replay(ticker, date) (C2) deterministic and
                           lookahead-free.

Any function that receives a DataContext and calls re_engine/yfinance/FRED/
mm_journal directly instead of going through ctx is a C1 violation -- that
was the entire point of this file existing.
"""
from __future__ import annotations

import os
import sys
from typing import Protocol

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import research_engine as re_engine  # noqa: E402


class DataContext(Protocol):
    def close(self, ticker: str) -> pd.Series:
        """Daily close series for `ticker`, full available history."""
        ...

    def ohlcv(self, ticker: str) -> pd.DataFrame:
        """Daily OHLCV DataFrame for `ticker` (open/high/low/close/volume)."""
        ...

    def vix(self) -> pd.Series:
        """VIX close series."""
        ...

    def hy_oas(self) -> pd.Series:
        """FRED high-yield OAS credit-spread series."""
        ...

    def episodes(self, ticker: str) -> list | None:
        """Annotated episode library for `ticker` (cause/what_ended_it),
        or None if unavailable -- fails soft, same posture as every other
        optional enrichment in this pipeline."""
        ...


class LiveDataContext:
    """Wraps research_engine's live fetches + an injected mm_journal
    callable. Memoized per (method, args) so repeated calls within one run
    -- e.g. compute_tearsheet_extras() resolving a benchmark ticker that's
    also the ticker being scored, or multiple engines wanting the same
    OHLCV frame -- fetch over the network at most once each. This
    memoization is also what makes the "no network call at compute time"
    test possible: prime the context (call its methods once, network
    allowed), then block the network and run compute -- if compute never
    bypasses the cache, it never touches the network either."""

    def __init__(self, mm_journal_fn=None, dry_run: bool = False):
        self._mm_journal_fn = mm_journal_fn
        self._dry_run = dry_run
        self._close_cache: dict[str, pd.Series] = {}
        self._ohlcv_cache: dict[str, pd.DataFrame] = {}
        self._episodes_cache: dict[str, list | None] = {}
        self._vix_cache: pd.Series | None = None
        self._hy_oas_cache: pd.Series | None = None

    def close(self, ticker: str) -> pd.Series:
        if ticker not in self._close_cache:
            self._close_cache[ticker] = re_engine.fetch_history(ticker)
        return self._close_cache[ticker]

    def ohlcv(self, ticker: str) -> pd.DataFrame:
        if ticker not in self._ohlcv_cache:
            self._ohlcv_cache[ticker] = re_engine.fetch_ohlcv(ticker)
        return self._ohlcv_cache[ticker]

    def vix(self) -> pd.Series:
        if self._vix_cache is None:
            self._vix_cache = re_engine.fetch_history("^VIX")
        return self._vix_cache

    def hy_oas(self) -> pd.Series:
        if self._hy_oas_cache is None:
            self._hy_oas_cache = re_engine.fetch_hy_oas()
        return self._hy_oas_cache

    def episodes(self, ticker: str) -> list | None:
        if ticker in self._episodes_cache:
            return self._episodes_cache[ticker]
        result = None
        if not self._dry_run and self._mm_journal_fn is not None:
            resp = self._mm_journal_fn("list_episodes", {"asset": ticker})
            result = (resp or {}).get("episodes")
        self._episodes_cache[ticker] = result
        return result
