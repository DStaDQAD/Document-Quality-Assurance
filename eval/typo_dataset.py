"""Labelled-case schema and loader for the typo-checker accuracy eval.

A case is a short piece of Indonesian narrative text plus what a correct checker should say
about it: which words must be flagged (`expect_flagged`) and which must be left alone
(`expect_clean`). Both sides matter, but they do not matter equally — a false accusation on a
word that was correct all along costs the reader more trust than a missed typo, because it
sends them to check something that was never wrong.

Scope, and why it is narrower than it looks
-------------------------------------------
The runner calls `check_typos(text, llm=None)`, which is the deterministic half of the
checker: the curated tidak-baku list, reduplication, and the prefix rules. In that mode a word
unknown to the id_ID dictionary is DROPPED rather than guessed (typo_checker.py tier 5
escalates it to the LLM, and there is no LLM), so a plain misspelling such as "Likuidiaftas"
is not reachable here at all.

That makes this harness the mirror image of Layer 1 for facts: it can hold PRECISION to
account with no API key and full reproducibility, and it can score recall for the rule-driven
categories, but recall on free-form misspellings belongs to an LLM-in-the-loop layer that does
not exist yet. Cases may still label such a word — it is recorded as a miss, which is the
honest reading, and keeps the gap visible instead of quietly excluded.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml


@dataclass(frozen=True)
class ExpectedIssue:
    """One word a correct checker must flag. `category`/`suggestion` are optional: when given
    they are checked too, when omitted the case only asserts that the word was flagged."""
    word: str
    category: Optional[str] = None    # ejaan | tidak_baku | grammar
    suggestion: Optional[str] = None


@dataclass(frozen=True)
class TypoCase:
    id: str
    text: str
    description: str = ""
    expect_flagged: List[ExpectedIssue] = field(default_factory=list)
    # Words present in the text that must NOT be flagged. Any flag on one of these already
    # counts as a false positive; naming them says which vocabulary the case is protecting.
    expect_clean: List[str] = field(default_factory=list)


def _parse_case(raw: dict) -> TypoCase:
    case_id = raw["id"]
    text = raw["text"]

    expect_flagged = [
        ExpectedIssue(
            word=e["word"],
            category=e.get("category"),
            suggestion=e.get("suggestion"),
        )
        for e in raw.get("expect_flagged", []) or []
    ]
    expect_clean = list(raw.get("expect_clean", []) or [])

    # A label naming a word the text does not contain can never be matched, so the case would
    # score a permanent miss (or protect nothing) while looking like a real case. Catch the
    # typo in the LABEL here rather than reading it as a finding about the checker.
    lowered = text.casefold()
    for word in [e.word for e in expect_flagged] + expect_clean:
        if word.casefold() not in lowered:
            raise ValueError(f"Case {case_id!r}: labelled word {word!r} does not occur in the text")

    return TypoCase(
        id=case_id,
        text=text,
        description=raw.get("description", ""),
        expect_flagged=expect_flagged,
        expect_clean=expect_clean,
    )


def load_typo_cases(paths: List[Path]) -> List[TypoCase]:
    """Load every case from the given YAML files (each file holds a list of cases).

    Raises ValueError on a duplicate case id so the report never conflates two cases.
    """
    cases: List[TypoCase] = []
    seen: Dict[str, str] = {}
    for path in paths:
        raw_docs = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        if not isinstance(raw_docs, list):
            raise ValueError(
                f"{path}: expected a top-level list of cases, got {type(raw_docs).__name__}"
            )
        for raw in raw_docs:
            case = _parse_case(raw)
            if case.id in seen:
                raise ValueError(f"Duplicate case id {case.id!r} in {path} (already in {seen[case.id]})")
            seen[case.id] = str(path)
            cases.append(case)
    return cases
