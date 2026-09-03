"""Inference for the trained classifiers.

Loads a pickled model and predicts a tier. The pickle carries the feature-name
list it was trained on and that list is checked against the current one at load
time: a reordering in dataset.py would otherwise mis-map every feature silently,
producing a model that is confidently wrong rather than obviously broken.
"""

from __future__ import annotations

import pickle
from pathlib import Path

from app.classifier.dataset import FEATURE_NAMES, INDEX_TO_LABEL, prompt_to_vector
from app.classifier.features import extract
from app.classifier.rules import Decision
from app.tiers.base import TierName

MODEL_DIR = Path(__file__).resolve().parents[2] / "models"


class ModelUnavailable(RuntimeError):
    """The requested model is missing, unreadable, or trained on other features."""


class MLClassifier:
    def __init__(self, name: str, model, features: tuple[str, ...]) -> None:
        self.name = name
        self._model = model
        self._features = features

    def classify(self, prompt: str) -> Decision:
        vector = prompt_to_vector(prompt)
        index = int(self._model.predict([vector])[0])
        tier = TierName(INDEX_TO_LABEL[index])

        reason = f"{self.name} prediction"
        proba = getattr(self._model, "predict_proba", None)
        if proba is not None:
            confidence = float(max(proba([vector])[0]))
            reason = f"{self.name} prediction (confidence {confidence:.2f})"
        return Decision(tier, reason, extract(prompt))


def load(name: str, model_dir: Path | None = None) -> MLClassifier:
    path = (model_dir or MODEL_DIR) / f"{name}.pkl"
    if not path.exists():
        raise ModelUnavailable(
            f"No trained model at {path}. Run `python -m app.classifier.train`, "
            "or set CLASSIFIER=rules."
        )
    try:
        with path.open("rb") as fh:
            payload = pickle.load(fh)
    except Exception as exc:  # noqa: BLE001 - any unpickling failure is the same problem
        raise ModelUnavailable(f"Could not load {path}: {exc}") from exc

    trained_on = tuple(payload.get("features", ()))
    if trained_on != FEATURE_NAMES:
        raise ModelUnavailable(
            f"{path} was trained on {trained_on}, but the current feature set is "
            f"{FEATURE_NAMES}. Retrain before using it."
        )
    return MLClassifier(name, payload["model"], trained_on)
