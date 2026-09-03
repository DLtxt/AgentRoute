"""Dataset schema, feature-vector mapping, and corpus generation."""

from __future__ import annotations

import pytest

from app.classifier.corpus import build_corpus
from app.classifier.dataset import (
    FEATURE_NAMES,
    INDEX_TO_LABEL,
    LABEL_TO_INDEX,
    LabeledPrompt,
    prompt_to_vector,
    read_dataset,
    to_xy,
    write_dataset,
)
from app.classifier.features import extract
from app.tiers.base import TierName


def test_feature_names_match_the_extractor_exactly():
    """A drift here would silently mis-map every feature at inference."""
    assert set(FEATURE_NAMES) == set(extract("hello").as_dict())


def test_vector_length_matches_feature_names():
    assert len(prompt_to_vector("hello world")) == len(FEATURE_NAMES)


def test_label_encoding_is_cheapest_first():
    """Confusion matrices should read in cost order."""
    assert LABEL_TO_INDEX["local"] == 0
    assert LABEL_TO_INDEX["haiku"] == 1
    assert LABEL_TO_INDEX["sonnet"] == 2
    assert INDEX_TO_LABEL[0] == "local"


def test_roundtrip_preserves_rows(tmp_path):
    rows = [
        LabeledPrompt("what is entropy?", TierName.LOCAL, "outcome"),
        LabeledPrompt("prove P != NP", TierName.SONNET, "outcome"),
    ]
    path = write_dataset(rows, tmp_path / "d.csv")
    back = read_dataset(path)
    assert [r.prompt for r in back] == [r.prompt for r in rows]
    assert [r.label for r in back] == [r.label for r in rows]


def test_missing_dataset_says_how_to_build_one(tmp_path):
    with pytest.raises(FileNotFoundError, match="label"):
        read_dataset(tmp_path / "nope.csv")


def test_to_xy_shapes_align():
    rows = [
        LabeledPrompt("a short one", TierName.LOCAL),
        LabeledPrompt("```code```", TierName.SONNET),
    ]
    x, y = to_xy(rows)
    assert len(x) == len(y) == 2
    assert y == [0, 2]


def test_corpus_is_deterministic_and_unique():
    a, b = build_corpus(120), build_corpus(120)
    assert a == b, "a re-run must label the same prompts"
    assert len(set(a)) == 120


def test_corpus_spans_all_three_tiers_under_the_rules_baseline():
    from app.classifier import rules

    tiers = {rules.classify(p).tier for p in build_corpus(300)}
    assert tiers == set(TierName), "a corpus that never reaches a tier cannot train for it"
