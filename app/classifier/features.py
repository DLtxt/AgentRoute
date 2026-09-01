"""Hand-crafted features. Shared by the rule-based and (later) ML classifiers.

Deliberately not embeddings: the point is to be able to point at a feature and
explain exactly why a prompt routed where it did.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

CODE_FENCE = re.compile(r"```")
SENTENCE_END = re.compile(r"[.!?]+")

COMPLEX_KEYWORDS = (
    "prove",
    "derive",
    "step by step",
    "explain why",
    "trade-off",
    "tradeoff",
    "architect",
    "design a",
    "analyze",
    "critique",
    "refactor",
)
CODE_KEYWORDS = (
    "write a function",
    "implement",
    "debug",
    "stack trace",
    "unit test",
    "regex",
    "sql",
)
SIMPLE_KEYWORDS = (
    "what is",
    "who is",
    "when did",
    "define",
    "translate",
    "summarize",
    "spell",
)


@dataclass(frozen=True)
class Features:
    char_length: int
    approx_tokens: int
    has_code_fence: bool
    sentence_count: int
    question_marks: int
    complex_keyword_hits: int
    code_keyword_hits: int
    simple_keyword_hits: int

    def as_dict(self) -> dict:
        return asdict(self)


def _count_hits(haystack: str, needles: tuple[str, ...]) -> int:
    return sum(1 for n in needles if n in haystack)


def extract(prompt: str) -> Features:
    lowered = prompt.lower()
    sentences = [s for s in SENTENCE_END.split(prompt) if s.strip()]
    return Features(
        char_length=len(prompt),
        approx_tokens=max(1, len(prompt) // 4),
        has_code_fence=bool(CODE_FENCE.search(prompt)),
        sentence_count=len(sentences),
        question_marks=prompt.count("?"),
        complex_keyword_hits=_count_hits(lowered, COMPLEX_KEYWORDS),
        code_keyword_hits=_count_hits(lowered, CODE_KEYWORDS),
        simple_keyword_hits=_count_hits(lowered, SIMPLE_KEYWORDS),
    )
