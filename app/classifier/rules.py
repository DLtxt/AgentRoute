"""Rule-based classifier (v1).

Routes to the cheapest tier plausibly capable of the prompt. Every decision
carries the reason that produced it, so `/query` can report why a prompt went
where it did — the same explainability the XGBoost feature-importance chart
buys later, available from day one.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.classifier.features import Features, extract
from app.tiers.base import TierName

LONG_PROMPT_TOKENS = 300
MEDIUM_PROMPT_TOKENS = 60


@dataclass(frozen=True)
class Decision:
    tier: TierName
    reason: str
    features: Features


def classify(prompt: str) -> Decision:
    f = extract(prompt)

    if f.has_code_fence:
        return Decision(TierName.SONNET, "contains a code fence", f)
    if f.complex_keyword_hits >= 2:
        return Decision(TierName.SONNET, "multiple reasoning keywords", f)
    if f.approx_tokens >= LONG_PROMPT_TOKENS:
        return Decision(TierName.SONNET, f"long prompt (>={LONG_PROMPT_TOKENS} tokens)", f)

    if f.complex_keyword_hits == 1:
        return Decision(TierName.HAIKU, "one reasoning keyword", f)
    if f.code_keyword_hits >= 1:
        return Decision(TierName.HAIKU, "code-related keyword", f)
    if f.approx_tokens >= MEDIUM_PROMPT_TOKENS or f.sentence_count > 3:
        return Decision(TierName.HAIKU, "medium-length or multi-sentence prompt", f)

    if f.simple_keyword_hits >= 1:
        return Decision(TierName.LOCAL, "simple lookup keyword", f)
    return Decision(TierName.LOCAL, "short, no complexity signal", f)
