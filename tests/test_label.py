"""Judge verdict parsing.

Both bugs covered here silently mislabeled data rather than erroring, which is
the worst failure mode for a dataset builder: the run looks like it worked.
"""

from __future__ import annotations

import re

import pytest

from app.classifier.label import TIER_SEQUENCE, JudgeUnusable, estimate_cost
from app.tiers.base import TierName


def parse(text: str) -> dict[TierName, bool]:
    """Mirror of the parsing in judge(), without the network call."""
    if not text.strip():
        raise JudgeUnusable("no text")
    verdicts: dict[TierName, bool] = {}
    for letter, tier in zip("ABC", TIER_SEQUENCE, strict=True):
        match = re.search(rf"^\s*{letter}\s*[:.\)-]\s*(\w+)", text, re.MULTILINE)
        if match:
            verdicts[tier] = match.group(1).upper().startswith("ACCEPT")
    return {tier: verdicts.get(tier, False) for tier in TIER_SEQUENCE}


def test_clean_verdicts_parse():
    v = parse("A: UNACCEPTABLE\nB: ACCEPTABLE\nC: ACCEPTABLE")
    assert v == {TierName.LOCAL: False, TierName.HAIKU: True, TierName.SONNET: True}


def test_preamble_does_not_shift_verdicts():
    """Positional parsing shifted every verdict by one whenever the judge
    prefaced its answer, mislabeling silently."""
    v = parse("Here are my gradings:\n\nA: ACCEPTABLE\nB: UNACCEPTABLE\nC: ACCEPTABLE")
    assert v[TierName.LOCAL] is True
    assert v[TierName.HAIKU] is False
    assert v[TierName.SONNET] is True


def test_alternative_separators_and_spacing():
    for text in (
        "A - ACCEPTABLE\nB - ACCEPTABLE\nC - ACCEPTABLE",
        "A) ACCEPTABLE\nB) ACCEPTABLE\nC) ACCEPTABLE",
        "  A:   ACCEPTABLE\n  B:   ACCEPTABLE\n  C:   ACCEPTABLE",
    ):
        assert all(parse(text).values()), text


def test_empty_judge_output_raises_instead_of_failing_everything():
    """Sonnet 5's adaptive thinking consumed the whole token budget and
    returned no text; that used to be read as 'every tier unacceptable' and
    skipped the prompt, which looked like the models failing."""
    with pytest.raises(JudgeUnusable):
        parse("")
    with pytest.raises(JudgeUnusable):
        parse("   \n  \n")


def test_missing_verdict_defaults_to_unacceptable():
    """Assuming success on a missing verdict would bias labels toward cheap."""
    v = parse("A: ACCEPTABLE\nB: ACCEPTABLE")
    assert v[TierName.SONNET] is False


def test_cost_estimate_scales_and_is_nonzero():
    assert estimate_cost(0) == 0
    assert estimate_cost(300) > estimate_cost(150) > 0
