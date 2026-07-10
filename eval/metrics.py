"""Classification metrics for verdict evaluation (pure functions, no I/O).

The verifier assigns one of three verdicts per fact, so evaluation is a 3-class
classification problem. For a fact-checker the class that matters most is 'Refuted'
(did we catch the wrong number?) — precision there means "when we cried wolf, was it
really wrong", recall means "of the truly wrong numbers, how many did we catch". Both
are reported per class rather than collapsed into a single accuracy, which can look
healthy while hiding a verifier that never flags anything.
"""

from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

VERDICTS: Tuple[str, ...] = ("Entailed", "Refuted", "Inconclusive")


@dataclass(frozen=True)
class ClassMetrics:
    """Precision/recall/F1 for a single verdict class."""
    label: str
    tp: int  # predicted this class AND expected this class
    fp: int  # predicted this class BUT expected another
    fn: int  # expected this class BUT predicted another
    support: int  # how many cases truly belong to this class (tp + fn)

    @property
    def precision(self) -> float:
        denom = self.tp + self.fp
        return self.tp / denom if denom else 0.0

    @property
    def recall(self) -> float:
        denom = self.tp + self.fn
        return self.tp / denom if denom else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0


@dataclass(frozen=True)
class EvalMetrics:
    total: int
    correct: int
    confusion: Dict[str, Dict[str, int]]  # expected -> predicted -> count
    per_class: Dict[str, ClassMetrics]

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0

    @property
    def macro_f1(self) -> float:
        if not self.per_class:
            return 0.0
        return sum(c.f1 for c in self.per_class.values()) / len(self.per_class)


def compute_metrics(
    pairs: Sequence[Tuple[str, str]], labels: Sequence[str] = VERDICTS
) -> EvalMetrics:
    """Compute confusion matrix and per-class metrics from (expected, predicted) pairs.

    Any label seen in the data that is not in `labels` is added, so an unexpected verdict
    string surfaces in the report instead of being silently dropped.
    """
    all_labels: List[str] = list(labels)
    for expected, predicted in pairs:
        for lbl in (expected, predicted):
            if lbl not in all_labels:
                all_labels.append(lbl)

    confusion: Dict[str, Dict[str, int]] = {
        e: {p: 0 for p in all_labels} for e in all_labels
    }
    correct = 0
    for expected, predicted in pairs:
        confusion[expected][predicted] += 1
        if expected == predicted:
            correct += 1

    per_class: Dict[str, ClassMetrics] = {}
    for lbl in all_labels:
        tp = confusion[lbl][lbl]
        fp = sum(confusion[e][lbl] for e in all_labels if e != lbl)
        fn = sum(confusion[lbl][p] for p in all_labels if p != lbl)
        per_class[lbl] = ClassMetrics(label=lbl, tp=tp, fp=fp, fn=fn, support=tp + fn)

    return EvalMetrics(
        total=len(pairs),
        correct=correct,
        confusion=confusion,
        per_class=per_class,
    )
