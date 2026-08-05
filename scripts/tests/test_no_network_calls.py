#!/usr/bin/env python3
"""
C1 acceptance test (Phase C): no network call may happen inside
forecast_engine.run_one() / compute_tearsheet_extras() / any engine when a
primed DataContext is supplied. This is the test that turns "engines don't
touch the network" from an assumption into something proven, per Phase C's
own acceptance criteria.

Method: construct a LiveDataContext and prime it (call every method
compute will need -- network allowed during priming), then block the
socket layer entirely and run a full run_one(). Any code that bypasses ctx
and reaches for the network directly raises immediately instead of
silently succeeding.

No pytest dependency (none is used anywhere else in this repo) -- plain
script, prints PASS/FAIL, exits non-zero on failure so it can gate CI the
same way every other scripts/*.py in this repo does.

Run: python3 scripts/tests/test_no_network_calls.py
"""
import os
import socket
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
sys.path.insert(0, SCRIPTS)
sys.path.insert(0, os.path.join(os.path.dirname(SCRIPTS), "engines"))

import forecast_engine as fe  # noqa: E402

TICKER = "SMH"


class NetworkBlocked(Exception):
    pass


def _blocked(*a, **k):
    raise NetworkBlocked(f"socket connection attempted with network blocked: args={a}")


def main():
    spy_close = fe.re_engine.fetch_history("SPY")
    spy_trend_df = fe.spy_trend_frame(spy_close)
    vix = fe.re_engine.fetch_history("^VIX")
    oas = fe.re_engine.fetch_hy_oas()
    universe_prices = {"SPY": spy_close, TICKER: fe.re_engine.fetch_history(TICKER)}
    analog_map = fe.load_analog_map()
    asset = {"ticker": TICKER, "label": TICKER}

    fe.mm_journal = lambda op, payload: None
    ctx = fe.LiveDataContext(mm_journal_fn=fe.mm_journal, dry_run=True)

    # Prime: touch every ctx method compute will need, network allowed.
    ctx.ohlcv(TICKER)
    ctx.vix()
    ctx.hy_oas()
    ctx.episodes(TICKER)
    for sym in fe.resolve_benchmarks(TICKER, analog_map).values():
        ctx.close(sym)

    # Block the network entirely, then run compute.
    real_connect = socket.socket.connect
    real_create_connection = socket.create_connection
    socket.socket.connect = _blocked
    socket.create_connection = _blocked
    try:
        result = fe.run_one(
            asset, universe_prices, spy_close, spy_trend_df, vix, oas,
            None, "closed", True, ctx, rotation_ctx=None,
            source="no_network_test", analog_map=analog_map,
        )
    except NetworkBlocked as e:
        print(f"FAIL: a network call escaped the DataContext seam: {e}")
        return 1
    finally:
        socket.socket.connect = real_connect
        socket.create_connection = real_create_connection

    if result is None:
        print("FAIL: run_one() returned None (unrelated failure -- check "
              "the [warn] output above); can't confirm the network-block "
              "assertion meaningfully.")
        return 1
    print("PASS: run_one() completed a real forecast with the network "
          "fully blocked after priming -- no compute-time network call.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
