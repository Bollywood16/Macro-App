#!/usr/bin/env python3
"""
refresh_replay_block_counts.py — nightly precompute for mm-journal's
`forecasts_replay_block_counts` read op.

WHY THIS EXISTS: `forecasts_replay_block_counts` used to run its 12
sequential COUNT(*) queries live, on every call. That was fine while
forecasts_replay was empty/small; now that the C4 backfill has landed the
full 17-ticker replay ledger (~530,000 rows, MARKET_MEMORY_V2_BUILD.md #4),
those 12 round trips reproducibly 500 the edge function
({"error":"db_error","detail":""}). This script calls a new op,
`refresh_forecasts_replay_block_counts`, which does the same 12 counts but
writes the result into a single summary row
(forecasts_replay_block_counts_summary, see the migration
20260807140000_forecasts_replay_block_counts_summary.sql) instead of
returning it inline. `forecasts_replay_block_counts` itself now just reads
that row -- cheap regardless of table size.

Run nightly via .github/workflows/replay-block-counts-refresh.yml, or by
hand: APP_PASSPHRASE=... python3 scripts/refresh_replay_block_counts.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import forecast_engine as fe  # noqa: E402

if __name__ == "__main__":
    resp = fe.mm_journal("refresh_forecasts_replay_block_counts", {})
    if resp is None or "block_counts" not in resp:
        print("[error] refresh_forecasts_replay_block_counts failed (see [warn] above)")
        sys.exit(1)
    print(f"computed_at={resp.get('computed_at')}")
    print(json.dumps(resp["block_counts"], indent=2))
