"""Rule-based routing decisions."""

from __future__ import annotations

import pytest

from app.classifier import rules
from app.classifier.features import extract
from app.tiers.base import TierName


@pytest.mark.parametrize(
    "prompt,expected",
    [
        ("What is the capital of France?", TierName.LOCAL),
        ("define entropy", TierName.LOCAL),
        ("```python\nprint(1)\n```", TierName.SONNET),
        ("Prove this and explain why, step by step.", TierName.SONNET),
        ("write a function that reverses a list", TierName.HAIKU),
    ],
)
def test_routing(prompt, expected):
    assert rules.classify(prompt).tier is expected


def test_long_prompt_routes_to_sonnet():
    assert rules.classify("word " * 1500).tier is TierName.SONNET


def test_decision_carries_a_reason():
    assert rules.classify("```code```").reason


def test_features_detect_code_fence_and_questions():
    f = extract("Why? How? ```x```")
    assert f.has_code_fence
    assert f.question_marks == 2
