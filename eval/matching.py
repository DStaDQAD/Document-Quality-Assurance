"""Match expected claims (labels) to the pipeline's actual FactVerificationResults.

Layer 2 runs the real LLM extraction, so the set of facts the pipeline returns never lines
up 1:1 with the labelled claims — some are missed, some are extra (spurious), and metric
names differ ("Uang Beredar Luas(M2)" vs "M2"). This module resolves that alignment so the
verdict metrics are computed only over claims that were actually found, while extraction
recall and spurious-fact count are reported as separate axes.

Matching is intentionally tolerant but anchored on the deterministic parts of a claim:
same operation, the claim's periods present among the result's periods (a subset, since
yoy adds the prior-year point the label doesn't list), and a containment match on the
metric name. First unused result wins; each result is matched at most once.

One exception to "first wins": a label may pin the number the PDF states (`claimed_value`),
and then a candidate stating that number is preferred over an earlier one that does not.
Documents repeat themselves — a summary bullet and a body paragraph make the same claim —
and when the two disagree they are the same fact to every other rule here, so the label
would otherwise be scored against whichever the extractor happened to emit first.
"""

import re
from dataclasses import dataclass, field
from typing import List, Tuple

from eval.e2e_dataset import ExpectedClaim
from schemas import FactVerificationResult


@dataclass
class MatchResult:
    matched: List[Tuple[ExpectedClaim, FactVerificationResult]] = field(default_factory=list)
    missing: List[ExpectedClaim] = field(default_factory=list)       # expected but not extracted
    spurious: List[FactVerificationResult] = field(default_factory=list)  # extracted but unlabelled


def _norm(text: str) -> str:
    """Lowercase and strip to alphanumerics for tolerant metric comparison."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _metric_matches(expected_metric: str, result_label: str) -> bool:
    e, r = _norm(expected_metric), _norm(result_label)
    if not e or not r:
        return False
    return e in r or r in e


def _periods_subset(claim: ExpectedClaim, result: FactVerificationResult) -> bool:
    want = {(p.year, p.month) for p in claim.periods}
    have = {(p.year, p.month) for p in result.periods}
    return want.issubset(have) if want else False


def _states_claimed_value(claim: ExpectedClaim, result: FactVerificationResult) -> bool:
    """True when the label pins a number and the result states that same number.

    Compared as printed: both sides come from the same decimal text, so any real difference
    is larger than the epsilon by orders of magnitude.
    """
    if claim.claimed_value is None or result.claimed_value is None:
        return False
    return abs(claim.claimed_value - result.claimed_value) < 1e-6


def match_results(
    claims: List[ExpectedClaim], results: List[FactVerificationResult]
) -> MatchResult:
    """Align expected claims with pipeline results (see module docstring for the rules)."""
    out = MatchResult()
    used: set = set()

    for claim in claims:
        candidates = [
            i for i, r in enumerate(results)
            if i not in used
            and r.operation == claim.operation
            and _periods_subset(claim, r)
            and _metric_matches(claim.metric, r.metric_label)
        ]
        chosen = next(
            (i for i in candidates if _states_claimed_value(claim, results[i])),
            candidates[0] if candidates else None,
        )
        if chosen is not None:
            used.add(chosen)
            out.matched.append((claim, results[chosen]))
        else:
            out.missing.append(claim)

    out.spurious = [r for i, r in enumerate(results) if i not in used]
    return out
