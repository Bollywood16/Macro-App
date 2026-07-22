"""
agreement_engine.py — robustness_final module D, with technicals folded in.

The "committee" is the app's model signals, NOT investor personas:
regime-analog, relative-strength, path-risk (Monte Carlo), trend, forecast —
now plus technicals as a 6th voter. Each emits a ballot; this engine measures
DISAGREEMENT (dispersion of votes, confidence-weighted) and its inverse,
CONVERGENCE, and fires both ways:
  - disagreement >= 0.60           -> WAIT (conflict; no edge)
  - high convergence + adequate    -> CONVERGENCE ALERT (rare high-conviction setup)
    independent-sample evidence

Calibration discipline (from robustness_final AND the prior critique):
uncalibrated voters carry weight 0 — they show on the card but do not move the
aggregate until scored against matured outcomes. Score thresholds here are
PLACEHOLDERS to be calibrated against the journal, not truths.
"""
from __future__ import annotations
from dataclasses import dataclass, field

VOTE_NUM = {"AVOID": -1.0, "WAIT": 0.0, "BUY": 1.0}

@dataclass
class Ballot:
    voter: str
    vote: str                 # BUY | WAIT | AVOID
    confidence: float         # 0..1
    independent_n: int        # effective sample (module A) behind this voter
    calibrated: bool = True   # uncalibrated -> weight 0

@dataclass
class AgreementResult:
    disagreement: float
    convergence: float
    effective_voters: int
    adequate_evidence: bool
    state: str                # 'CONVERGENCE ALERT' | 'CONFLICT / WAIT' | 'MIXED'
    card_line: str
    detail: list = field(default_factory=list)

def score(ballots: list[Ballot],
          disagree_gate: float = 0.60,
          converge_gate: float = 0.75,
          min_independent_n: int = 8,
          min_families: int = 4) -> AgreementResult:
    # weight 0 for uncalibrated voters: informative on the card, inert in the aggregate
    active = [(b, (b.confidence if b.calibrated else 0.0)) for b in ballots]
    wsum = sum(w for _, w in active)
    detail = [f"{b.voter}: {b.vote} (conf {b.confidence:.2f}, n={b.independent_n}"
              + ("" if b.calibrated else ", UNCALIBRATED w=0") + ")" for b, _ in active]
    if wsum == 0:
        return AgreementResult(0.0, 0.0, 0, False, "MIXED",
                               "No calibrated voters yet — advisory only.", detail)
    mean = sum(VOTE_NUM[b.vote] * w for b, w in active) / wsum
    var = sum(w * (VOTE_NUM[b.vote] - mean) ** 2 for b, w in active) / wsum
    disagreement = round(min(var, 1.0), 3)          # 0 (all agree) .. 1 (max split)
    convergence = round(1.0 - disagreement, 3)
    families = [b for b, w in active if w > 0 and b.independent_n >= min_independent_n]
    adequate = len(families) >= min_families
    if convergence >= converge_gate and adequate:
        state = "CONVERGENCE ALERT"
        card = (f"CONVERGENCE {convergence:.0%} across {len(families)} independent "
                f"model families — rare high-conviction setup.")
    elif disagreement >= disagree_gate:
        state = "CONFLICT / WAIT"
        card = f"Models disagree ({disagreement:.0%}) — no edge; WAIT."
    else:
        state = "MIXED"
        card = f"Partial agreement (convergence {convergence:.0%}); not yet actionable."
    return AgreementResult(disagreement, convergence, len(families), adequate,
                           state, card, detail)
