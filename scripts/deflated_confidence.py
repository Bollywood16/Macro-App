"""
Deflated-confidence adjustment — Stage 3b of the Model Upgrade directive.

Every claim-mining pass in this app (research_engine.py, rotation_engine.py)
tests many regime conjunctions and only reports the ones that clear a
minimum sample size. That is exactly the multiple-testing setup Bailey &
Lopez de Prado's Deflated Sharpe Ratio (DSR) was built for: the more trials
you run, the higher a "significant-looking" result the BEST of them will
show by chance alone, even under a true null of no real edge. DSR handles
this by comparing an observed Sharpe ratio against the EXPECTED MAXIMUM
Sharpe ratio you'd see from N independent trials under the null (an
extreme-value-theory approximation), rather than against zero.

This module applies the same logic to this app's claims, which aren't
Sharpe ratios but ARE directional hit-rate statistics ("consistency" — the
share of episodes agreeing with the claim's sign), so the same
expected-max-under-N-trials benchmark applies directly:

  1. Convert the claim's consistency into a z-score against the null of no
     directional edge (p=0.5), via a one-sample proportion test.
  2. Compute the expected max z-score you'd see from `searched` independent
     trials under that same null: z_chance_max ~= sqrt(2 * ln(searched))
     (the standard extreme-value approximation for the max of N iid
     standard normals — the same asymptotic underlying DSR's own
     expected-max-Sharpe benchmark).
  3. A claim only keeps confidence in proportion to how far its own z-score
     clears that chance-max bar — not for merely being positive.

This REPLACES the old approach of reporting `conjunctions_searched` as
metadata and relying on a prompt instruction ("weigh claims against that
denominator") to informally discount mined claims. The discount is now a
number computed here, not a request to an LLM to eyeball it.
"""

import math

# Existing per-file base-score weights (sample size / consistency / depth /
# decade-coverage), unchanged from before this module existed — this file
# only adds the multiple-testing deflation multiplier on top.


def z_score(n: int, consistency: float) -> float:
    """One-sample proportion z-score for `consistency` (share of episodes
    agreeing with the claim's sign) against the null of no edge (p=0.5)."""
    if n <= 0:
        return 0.0
    se = math.sqrt(0.25 / n)
    return (consistency - 0.5) / se if se > 0 else 0.0


def expected_max_z_under_chance(searched: int) -> float:
    """Expected max z-score from `searched` independent trials under the
    null, via the standard extreme-value approximation for the max of N
    iid standard normals. searched < 2 has no meaningful "best of many"
    effect, so it's floored at 2 (a single trial has no multiple-testing
    inflation to correct for)."""
    n = max(int(searched), 2)
    return math.sqrt(2 * math.log(n))


def deflation_factor(n: int, consistency: float, searched: int) -> float:
    """0..1 multiplier: 0 if the claim's own z-score doesn't even clear the
    expected best-of-`searched`-trials bar under pure chance (indistinguish-
    able from mining noise), approaching 1 as it clears that bar by an
    increasingly wide margin."""
    z_obs = z_score(n, consistency)
    if z_obs <= 0:
        return 0.0
    z_chance = expected_max_z_under_chance(searched)
    excess = max(0.0, z_obs - z_chance)
    return excess / (excess + z_chance) if (excess + z_chance) > 0 else 0.0


def deflated_confidence(n: int, consistency: float, depth: int, decades: int,
                         searched: int):
    """Base score (sample size x consistency x conjunction-depth penalty x
    decade-coverage — the pre-existing formula) times the new multiple-
    testing deflation factor. Returns (score_0_100, label)."""
    base = (100 * min(1, n / 12) * consistency * (0.85 ** depth)
            * min(1, decades / 4))
    deflate = deflation_factor(n, consistency, searched)
    score = round(base * deflate)
    label = ("high" if score >= 70 else
              "moderate" if score >= 40 else "low / likely mined")
    return score, label
