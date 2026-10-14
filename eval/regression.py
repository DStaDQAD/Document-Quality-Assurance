"""Regression corpus: which documents to check, what is known to be true about them, and how
two runs of the same documents differ claim by claim.

The rule this exists for (docs/audit-sep2026-next.md, "Aturan main" #2): a change may turn
Tidak Cukup Data into Sesuai, but every Sesuai that turns into anything else, and every new
Tidak Sesuai, has to be explained one by one. Aggregate counts hide that, so the diff pairs
claims individually. The runner (eval/run_regression.py) does the expensive part.
"""

import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union

import yaml

VERDICTS = ("Entailed", "Refuted", "Inconclusive")
_MODES = ("excel", "internal", "both")


@dataclass(frozen=True)
class Label:
    """A claim whose truth was checked by hand against the tables.

    `quote` is a fragment of the sentence that makes the claim — results are found through
    their context_quote, which is recovered from the document itself and therefore stable from
    run to run, unlike the metric label the extractor invents.
    """
    quote: str
    expected: str
    operation: Optional[str] = None
    claimed_value: Optional[float] = None
    note: str = ""


@dataclass(frozen=True)
class Source:
    file: str
    sheets: Union[str, List[str]] = "*"   # "*" = every sheet of the workbook


@dataclass(frozen=True)
class Document:
    id: str
    publication: str
    doc: str
    sources: List[Source]
    mode: str = "excel"
    labels: List[Label] = field(default_factory=list)


def _parse_label(doc_id: str, raw: dict) -> Label:
    if raw.get("expected") not in VERDICTS:
        raise ValueError(f"{doc_id}: label expected {raw.get('expected')!r} is not one of {VERDICTS}")
    return Label(quote=raw["quote"], expected=raw["expected"], operation=raw.get("operation"),
                 claimed_value=raw.get("claimed_value"), note=raw.get("note", ""))


def _parse_document(raw: dict) -> Document:
    doc_id = raw["id"]
    mode = raw.get("mode", "excel")
    if mode not in _MODES:
        raise ValueError(f"{doc_id}: unknown mode {mode!r}")
    sources = [Source(file=s["file"], sheets=s.get("sheets", "*")) for s in raw.get("sources", [])]
    if mode in ("excel", "both") and not sources:
        raise ValueError(f"{doc_id}: mode {mode!r} needs at least one workbook")
    return Document(id=doc_id, publication=raw["publication"], doc=raw["doc"], sources=sources,
                    mode=mode, labels=[_parse_label(doc_id, l) for l in raw.get("labels", [])])


def load_manifest(paths: Sequence[Path]) -> List[Document]:
    docs: List[Document] = []
    seen = set()
    for path in paths:
        raw_docs = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or []
        if not isinstance(raw_docs, list):
            raise ValueError(f"{path}: expected a top-level list of documents")
        for raw in raw_docs:
            doc = _parse_document(raw)
            if doc.id in seen:
                raise ValueError(f"Duplicate document id {doc.id!r}")
            seen.add(doc.id)
            docs.append(doc)
    return docs


def _norm(text: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


# --- scoring labels ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LabelScore:
    label: Label
    status: str            # benar | salah | hilang
    verdicts: List[str]


def _label_matches(label: Label, result: dict) -> bool:
    if _norm(label.quote) not in _norm(result.get("context_quote")):
        return False
    if label.operation and result.get("operation") != label.operation:
        return False
    if label.claimed_value is not None:
        value = result.get("claimed_value")
        if value is None or abs(value - label.claimed_value) > 1e-6:
            return False
    return True


def score_label(label: Label, results: List[dict]) -> LabelScore:
    """Right / wrong / missing for one hand-checked claim.

    A claim may come back as several facts (one per group named in the sentence). A true claim
    is right only when EVERY fact reached the expected verdict; a seeded error is caught as soon
    as ANY fact is Tidak Sesuai, because the groups the error did not touch are rightly Sesuai.
    """
    verdicts = [r.get("verdict") for r in results if _label_matches(label, r)]
    if not verdicts:
        status = "hilang"
    elif label.expected == "Refuted":
        status = "benar" if "Refuted" in verdicts else "salah"
    else:
        status = "benar" if all(v == label.expected for v in verdicts) else "salah"
    return LabelScore(label=label, status=status, verdicts=verdicts)


# --- diffing two runs -------------------------------------------------------------------------

@dataclass(frozen=True)
class Change:
    kind: str                  # sama | berubah | hilang | baru | dibentuk_ulang
    base: Optional[dict]
    new: Optional[dict]

    @property
    def needs_review(self) -> bool:
        """Rule #2: a Sesuai lost (changed or gone) or a Tidak Sesuai gained must be explained."""
        if self.kind == "dibentuk_ulang":
            return False
        before = self.base.get("verdict") if self.base else None
        after = self.new.get("verdict") if self.new else None
        if before == after:
            return False
        return before == "Entailed" or after == "Refuted"

    @property
    def ai_review(self) -> bool:
        return any("Ditinjau AI" in ((r or {}).get("reasoning") or "") for r in (self.base, self.new))


def _key(r: dict) -> Tuple:
    value = r.get("claimed_value")
    return (r.get("operation"), None if value is None else round(value, 4), _norm(r.get("context_quote")))


def _similarity(a: Optional[str], b: Optional[str]) -> float:
    return difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def _pair(base: List[dict], new: List[dict], same_group) -> Tuple[List[Tuple[dict, dict]], List[dict], List[dict]]:
    """Greedy pairing inside groups that `same_group` says may be the same claim; within a
    group the closest metric labels are paired first."""
    candidates = []
    for i, b in enumerate(base):
        for j, n in enumerate(new):
            if same_group(b, n):
                candidates.append((-_similarity(b.get("metric_label"), n.get("metric_label")), i, j))
    candidates.sort()
    used_b, used_n, pairs = set(), set(), []
    for _, i, j in candidates:
        if i not in used_b and j not in used_n:
            used_b.add(i)
            used_n.add(j)
            pairs.append((i, j))
    pairs.sort()
    return ([(base[i], new[j]) for i, j in pairs],
            [b for i, b in enumerate(base) if i not in used_b],
            [n for j, n in enumerate(new) if j not in used_n])


def diff_runs(base: List[dict], new: List[dict]) -> List[Change]:
    """Pair the claims of two runs of one document and say how each one moved.

    Same claim = same operation, same stated number and same sentence. A second pass accepts a
    near-identical sentence (an anchor quoted slightly differently falls back to the anchor
    itself), still requiring the operation and number to agree.
    """
    exact, rest_b, rest_n = _pair(base, new, lambda b, n: _key(b) == _key(n))
    loose, rest_b, rest_n = _pair(
        rest_b, rest_n,
        lambda b, n: _key(b)[:2] == _key(n)[:2]
        and _similarity(b.get("context_quote"), n.get("context_quote")) >= 0.6,
    )
    # A quote that fell back to the model's anchor ('sebesar 109,2') in one run and was located
    # in the other: same operation, number and metric label is the same claim.
    by_label, rest_b, rest_n = _pair(
        rest_b, rest_n,
        lambda b, n: _key(b)[:2] == _key(n)[:2]
        and _norm(b.get("metric_label")) == _norm(n.get("metric_label")),
    )
    changes = [Change("sama" if b.get("verdict") == n.get("verdict") else "berubah", b, n)
               for b, n in exact + loose + by_label]
    # A claim gone from one run whose sentence the other run checks with facts of another shape
    # (five per-group facts -> one "seluruh kelompok" fact), all reaching the same verdict, is
    # the same check restructured — not a lost Sesuai or a new finding.
    changes += [Change("dibentuk_ulang" if _restructured(b, base, new) else "hilang", b, None)
                for b in rest_b]
    changes += [Change("dibentuk_ulang" if _restructured(n, new, base) else "baru", None, n)
                for n in rest_n]
    return changes


def _same_sentence(a: str, b: str) -> bool:
    return len(a) > 15 and len(b) > 15 and (a in b or b in a)


def _sentence_verdicts(quote: str, run: List[dict]) -> set:
    return {r.get("verdict") for r in run if _same_sentence(quote, _norm(r.get("context_quote")))}


def _restructured(claim: dict, own_run: List[dict], other_run: List[dict]) -> bool:
    """The other run checks this claim's sentence, reaches the claim's verdict there, and
    reaches no verdict on it that this run did not (a fact both runs leave Tidak Cukup Data
    does not stop a restructured sentence from counting as the same check)."""
    quote = _norm(claim.get("context_quote"))
    other = _sentence_verdicts(quote, other_run)
    return claim.get("verdict") in other and other <= _sentence_verdicts(quote, own_run)


def summarize(changes: List[Change]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for c in changes:
        out[c.kind] = out.get(c.kind, 0) + 1
    out["perlu_dijelaskan"] = sum(c.needs_review for c in changes)
    return out
