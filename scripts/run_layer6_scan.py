#!/usr/bin/env python3
"""
Daily Layer 6 batch scan -- runs mm_tools.run_layer6() against the top
setups from today's data/scanner_digest.json (the Daily Scanner's
already opportunity_score-ranked shortlist, reused rather than
re-derived) and writes data/layer6_digest.json.

Kept as a separate driver rather than a third mm_tools.py subcommand --
mm_tools.py's CLI surface is exactly the two subcommands (layer6, lookup)
the directive specified; this script is the "wire it into a daily batch"
layer on top, the same relationship scanner.py has to forecast_engine.py
(imports and loops over a universe, doesn't re-implement the per-ticker
logic).

mm-tools.yml schedules this at 22:30 UTC, 5 minutes after scanner.yml's
22:25 UTC run, so it normally reads TODAY's scanner digest. If that run
was late or failed, this still reads WHATEVER scanner digest is
currently committed rather than blocking -- same "operate on latest
committed state, label it, never silently backfill" discipline as every
other engine here. See meta.scanner_digest_generated_utc in the output
for that timestamp, which makes staleness visible instead of silent.

Fail-soft: a ticker whose layer6 call raises unexpectedly (mm_tools.
run_layer6 already catches the fetch/data-shape failures it can predict)
is recorded with an error entry, never dropped silently, and never lets
one bad ticker abort the rest of the batch.
"""

import json
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import mm_tools  # noqa: E402

SCANNER_DIGEST_PATH = os.path.join(ROOT, "data", "scanner_digest.json")
OUTPUT_PATH = os.path.join(ROOT, "data", "layer6_digest.json")
TOP_N = 5
INTERVAL = "15m"


def load_scanner_digest():
    try:
        with open(SCANNER_DIGEST_PATH) as f:
            return json.load(f)
    except Exception as e:
        print(f"[warn] could not read scanner digest: {e}")
        return {}


def main():
    scanner_digest = load_scanner_digest()
    setups = scanner_digest.get("setups") or []  # already ranked -opportunity_score
    tickers = [s["ticker"] for s in setups[:TOP_N]]

    results = []
    for t in tickers:
        try:
            results.append(mm_tools.run_layer6(t, INTERVAL))
        except Exception as e:
            results.append({"ticker": t, "interval": INTERVAL, "verdict": "NO-GO",
                             "facts": {}, "gates": {},
                             "warnings": [f"unexpected_error: {e}"]})

    digest = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "source": "top setups from data/scanner_digest.json, opportunity_score-ranked",
        "scanner_digest_generated_utc": (scanner_digest.get("meta") or {}).get("generated_utc"),
        "interval": INTERVAL,
        "top_n": TOP_N,
        "scanned_tickers": tickers,
        "results": results,
    }
    os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(digest, f, indent=2, default=str)
    go_count = sum(1 for r in results if r.get("verdict") == "GO")
    print(f"layer6 batch: {len(tickers)} tickers scanned, {go_count} GO")


if __name__ == "__main__":
    sys.exit(main())
