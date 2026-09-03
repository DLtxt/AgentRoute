"""Pick the active classifier from configuration.

All three implementations expose the same `classify(prompt) -> Decision`, which
is what makes CLASSIFIER=rules|logreg|xgb an A/B switch at runtime rather than a
code change. Comparing them on live traffic is the point.
"""

from __future__ import annotations

import logging

from app.classifier import ml, rules
from app.classifier.rules import Decision

log = logging.getLogger("classifier")

VALID = ("rules", "logreg", "xgb")


class RuleClassifier:
    name = "rules"

    def classify(self, prompt: str) -> Decision:
        return rules.classify(prompt)


def build_classifier(name: str, *, fallback_to_rules: bool = True):
    if name not in VALID:
        raise ValueError(f"CLASSIFIER must be one of {VALID}, got {name!r}")
    if name == "rules":
        return RuleClassifier()
    try:
        classifier = ml.load(name)
        log.info("classifier.loaded", extra={"classifier": name})
        return classifier
    except ml.ModelUnavailable as exc:
        if not fallback_to_rules:
            raise
        # Falling back keeps the gateway serving on a fresh clone where no
        # model has been trained yet. Logged at warning so it cannot pass
        # unnoticed -- a silent downgrade would make an A/B comparison
        # meaningless without anyone realising.
        log.warning(
            "classifier.fallback",
            extra={"requested": name, "using": "rules", "reason": str(exc)},
        )
        return RuleClassifier()
