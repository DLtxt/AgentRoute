"""Classifier selection, model loading, and the fallback path."""

from __future__ import annotations

import pickle

import pytest

from app.classifier import ml
from app.classifier.select import VALID, build_classifier
from app.tiers.base import TierName


def test_rules_classifier_is_always_available():
    c = build_classifier("rules")
    assert c.name == "rules"
    assert c.classify("what is entropy?").tier is TierName.LOCAL


def test_unknown_classifier_name_is_rejected():
    with pytest.raises(ValueError, match="CLASSIFIER"):
        build_classifier("magic")


def test_valid_names_are_the_documented_three():
    assert VALID == ("rules", "logreg", "xgb")


def test_missing_model_falls_back_to_rules_rather_than_failing(monkeypatch, tmp_path):
    monkeypatch.setattr(ml, "MODEL_DIR", tmp_path)
    c = build_classifier("xgb")
    assert c.name == "rules", "a fresh clone with no model must still serve"


def test_missing_model_can_be_made_fatal():
    with pytest.raises(ml.ModelUnavailable, match="train"):
        ml.load("xgb", model_dir=__import__("pathlib").Path("/nonexistent"))


def test_model_trained_on_different_features_is_refused(tmp_path):
    """Silently mis-mapping features would give a confidently wrong model."""
    path = tmp_path / "xgb.pkl"
    with path.open("wb") as fh:
        pickle.dump({"model": object(), "features": ("wrong", "features")}, fh)
    with pytest.raises(ml.ModelUnavailable, match="Retrain"):
        ml.load("xgb", model_dir=tmp_path)


def test_corrupt_model_file_is_reported_clearly(tmp_path):
    (tmp_path / "logreg.pkl").write_bytes(b"not a pickle")
    with pytest.raises(ml.ModelUnavailable, match="Could not load"):
        ml.load("logreg", model_dir=tmp_path)
