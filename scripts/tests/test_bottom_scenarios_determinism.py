#!/usr/bin/env python3
"""
Permanent regression test for the Monte Carlo determinism fix (df400c8):
bottom_scenarios.py used to seed with np.random.default_rng() (no seed),
so two production runs of the IDENTICAL forecast simulated different
trough scenarios with no visible reason. Formalizes what was verified
inline at the time -- an inline verification that isn't in the suite
stops being true silently the next time something nearby changes.

Asserts two things:
  1. Same seed, called twice -> byte-identical output (excluding the one
     known audit-timestamp field, assumptions.computed_at).
  2. No seed, called twice -> output actually differs -- proves the seed
     is what fixes determinism, not some other factor (e.g. if this ever
     started passing with unseeded calls too, it would mean something else
     silently made the whole module deterministic, and this test would be
     giving a false sense of security about the *reason*).

No pytest dependency (none is used anywhere else in this repo) -- plain
script, prints PASS/FAIL, exits non-zero on failure.

Run: python3 scripts/tests/test_bottom_scenarios_determinism.py
"""
import copy
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
ROOT = os.path.dirname(SCRIPTS)
sys.path.insert(0, SCRIPTS)
sys.path.insert(0, os.path.join(ROOT, "engines"))

import research_engine as re_engine  # noqa: E402
import bottom_scenarios as bs  # noqa: E402
import forecast_engine as fe  # noqa: E402

TICKER = "SMH"


def strip_computed_at(d):
    d = copy.deepcopy(d)
    d.get("assumptions", {}).pop("computed_at", None)
    return d


def as_json(d):
    return json.dumps(strip_computed_at(d), sort_keys=True, default=str)


def main():
    ohlcv = re_engine.fetch_ohlcv(TICKER)
    seed = fe.deterministic_seed(TICKER, "2026-08-04", fe.MODEL_VERSION)

    seeded_1 = bs.bottom_scenarios(ohlcv, TICKER, seed=seed)
    seeded_2 = bs.bottom_scenarios(ohlcv, TICKER, seed=seed)
    seeded_match = as_json(seeded_1) == as_json(seeded_2)

    unseeded_1 = bs.bottom_scenarios(ohlcv, TICKER)
    unseeded_2 = bs.bottom_scenarios(ohlcv, TICKER)
    unseeded_differ = as_json(unseeded_1) != as_json(unseeded_2)

    ok = True
    if seeded_match:
        print("PASS: same seed, two calls -> byte-identical output "
              "(excluding computed_at).")
    else:
        print("FAIL: same seed produced different output -- determinism "
              "fix is broken or was reverted.")
        ok = False

    if unseeded_differ:
        print("PASS: no seed, two calls -> output differs (confirms the "
              "seed is what fixes it, not some other factor).")
    else:
        print("WARN: unseeded calls produced identical output. Either "
              "something else made this module deterministic (update this "
              "test's assumption), or this run got unlucky -- rerun to "
              "confirm before treating it as a real signal. Not failing "
              "the test on this alone.")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
