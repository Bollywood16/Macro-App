#!/usr/bin/env python3
"""
Outcome Scoring — Market Memory M4 (spec 9.2).

Finds forecasts that have matured (their horizon has actually elapsed in
TRADING days, not calendar days) and don't have an outcome row yet, and
writes one via the new mm-journal `create_outcome` op. This is what turns
"what did the model say" into "what actually happened" for the Journal's
history bucket (MVP item 6).

TRADING-CALENDAR MATURITY, WITHOUT A CALENDAR LIBRARY
A forecast's own ticker's price series IS a trading calendar for that
ticker — the same "position + horizon" bar-counting convention already
used by forecast_engine.horizon_stats / research_engine.build_episode.
`trading_date` (B6: the actual trading day this forecast's price/features
are anchored to — never `as_of_ts`, which is wall-clock execution time and
can legitimately land on a different calendar day, confirmed for 92% of
rows) is located in the freshly-fetched close series (searchsorted to the
first bar on/after that date — for a manual/intraday forecast this is the
same-day synthetic bar's real close, once time has passed); the forecast
is matured once `horizon_days` further bars exist past that position.
Not-yet-matured rows are left alone and re-checked next run — this job is
idempotent and safe to run daily regardless of whether anything is
actually due.

FIELDS (mirrors the `outcomes` table exactly, db/001_market_memory_schema.sql)
  end_price / abs_return / max_adverse_exc / max_favorable_exc — from the
    matured window, entry normalized against the forecast's own
    effective_price (what the recommendation card actually promised), not
    just whatever the ticker's close happened to be that day.
  benchmark_return / excess_return — SPY over the same window, aligned to
    the ticker's own trading calendar (same reindex+ffill pattern as
    forecast_engine.build_feature_frame's rel_spy_63d).
  event_occurred = abs_return > 0 — matches the forecast's own p_positive
    target definition (P(forward return > 0)).
  interval_covered = q20 <= abs_return <= q80.
  brier = (p_positive - event_occurred)^2.
  log_loss = standard binary log loss, p_positive clipped away from 0/1.
"""

import math
import os
import sys
from datetime import datetime, timezone

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import forecast_engine as fe  # noqa: E402

PENDING_LIMIT = 500
LOG_LOSS_EPS = 1e-6


def find_entry_pos(close: pd.Series, trading_date: str):
    # B6 fix: locate the entry bar by trading_date (the actual trading day
    # this forecast's price/features are anchored to -- see
    # 20260805014219_trading_date_column.sql), never as_of_ts. as_of_ts is
    # wall-clock execution time and can legitimately land on a different
    # calendar day (weekend or not) than the data it's about -- searching
    # against it lands on the wrong bar and shifts the whole scoring
    # window. Confirmed live: 92% of forecast rows have as_of_ts::date !=
    # the correct trading day.
    try:
        td = pd.Timestamp(trading_date)
    except Exception:
        return None
    if td.tzinfo is not None:
        td = td.tz_localize(None)
    pos = close.index.searchsorted(td.normalize())
    if pos >= len(close):
        return None
    return int(pos)


def score_forecast(row, close: pd.Series, spy_aligned: pd.Series):
    horizon = row["horizon_days"]
    entry_pos = find_entry_pos(close, row["trading_date"])
    if entry_pos is None:
        return None
    end_pos = entry_pos + horizon
    if end_pos >= len(close):
        return None  # not matured yet

    entry = float(row["effective_price"])
    if entry <= 0:
        return None
    window = close.iloc[entry_pos:end_pos + 1] / entry - 1
    abs_return = float(window.iloc[-1])
    max_adverse = float(window.min())
    max_favorable = float(window.max())
    end_price = float(close.iloc[end_pos])

    benchmark_return = None
    se, ee = spy_aligned.iloc[entry_pos], spy_aligned.iloc[end_pos]
    if pd.notna(se) and pd.notna(ee) and se > 0:
        benchmark_return = float(ee / se - 1)
    excess_return = (abs_return - benchmark_return) if benchmark_return is not None else None

    event_occurred = abs_return > 0
    q20, q80 = row.get("q20"), row.get("q80")
    interval_covered = (q20 is not None and q80 is not None
                         and q20 <= abs_return <= q80)

    p = row.get("p_positive")
    brier = log_loss = None
    if p is not None:
        y = 1.0 if event_occurred else 0.0
        brier = (p - y) ** 2
        pc = min(max(p, LOG_LOSS_EPS), 1 - LOG_LOSS_EPS)
        log_loss = -(y * math.log(pc) + (1 - y) * math.log(1 - pc))

    return {
        "forecast_id": row["id"],
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "end_price": round(end_price, 6),
        "benchmark_return": round(benchmark_return, 6) if benchmark_return is not None else None,
        "abs_return": round(abs_return, 6),
        "excess_return": round(excess_return, 6) if excess_return is not None else None,
        "max_adverse_exc": round(max_adverse, 6),
        "max_favorable_exc": round(max_favorable, 6),
        "event_occurred": bool(event_occurred),
        "interval_covered": bool(interval_covered) if (q20 is not None and q80 is not None) else None,
        "brier": round(brier, 6) if brier is not None else None,
        "log_loss": round(log_loss, 6) if log_loss is not None else None,
    }


def main():
    resp = fe.mm_journal("list_pending_outcomes", {"limit": PENDING_LIMIT})
    pending = (resp or {}).get("forecasts") or []
    if not pending:
        print("no pending forecasts to evaluate")
        return 0

    by_ticker = {}
    for row in pending:
        by_ticker.setdefault(row["ticker"], []).append(row)

    spy_close = fe.re_engine.fetch_history("SPY")

    # B3 guard: list_pending_outcomes now reads the pending_outcomes view
    # (redefined by 20260805014219_trading_date_column.sql to join on
    # trading_date, not as_of_ts -- see B6), which already pre-filters to
    # trading_date + horizon_days calendar-elapsed AND no existing outcome
    # row -- so `pending` here is never "obviously too fresh" or "already
    # resolved" by construction. not_yet_matured still happens (the view's
    # calendar-day filter is a lower bound, not the real trading-day bar
    # count -- see the view's own comment), so it isn't counted as a
    # failure. persist_errored is: score_forecast confirmed a real,
    # matured outcome and mm_journal's create_outcome call failed to save
    # it. That combination -- a real outcome computed, zero actually
    # persisted -- is exactly the failure mode that let this resolver
    # silently stall for three weeks (list_pending_outcomes kept handing
    # back the same already-resolved rows, which duplicate-keyed on every
    # create_outcome call once outcomes_forecast_uniq existed). Catching
    # it here means any future recurrence -- of this bug or a new one --
    # fails the run loudly instead of posting a green checkmark.
    matured, not_yet, scoring_errored, persist_errored, fetch_errored = 0, 0, 0, 0, 0
    for ticker, rows in by_ticker.items():
        try:
            close = fe.re_engine.fetch_history(ticker)
        except Exception as e:
            print(f"[warn] skipping {ticker}: {e}")
            fetch_errored += len(rows)
            continue
        spy_aligned = close if ticker == "SPY" else spy_close.reindex(close.index).ffill()

        for row in rows:
            try:
                outcome = score_forecast(row, close, spy_aligned)
            except Exception as e:
                print(f"[warn] {ticker} forecast {row.get('id')}: scoring failed: {e}")
                scoring_errored += 1
                continue
            if outcome is None:
                not_yet += 1
                continue
            resp = fe.mm_journal("create_outcome", outcome)
            if resp is None or "outcome" not in resp:
                print(f"[warn] {ticker} forecast {row['id']}: create_outcome failed")
                persist_errored += 1
            else:
                matured += 1

    print(f"pending={len(pending)} matured_and_scored={matured} "
          f"not_yet_matured={not_yet} scoring_errored={scoring_errored} "
          f"persist_errored={persist_errored} fetch_errored={fetch_errored}")

    if matured == 0 and persist_errored > 0:
        print(f"[error] {persist_errored} forecast(s) scored as genuinely "
              f"matured this run but none persisted -- treating this as a "
              f"failed run rather than a quiet no-op.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
