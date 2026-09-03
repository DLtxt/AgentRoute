"""Labeled dataset: schema, IO, and the feature matrix the models train on.

The label is *the cheapest tier that produced an acceptable answer*, decided by
running a prompt through every tier and comparing outputs -- not by guessing
which tier a prompt "looks like" it needs. That distinction is the whole point:
a model trained on intuition labels only learns to reproduce the intuition,
which the rule-based classifier already encodes for free.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from app.classifier.features import Features, extract
from app.tiers.base import TierName

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
LABELED_PATH = DATA_DIR / "labeled_prompts.csv"

# Column order is also the feature vector order. Models are saved alongside
# this list so a reordering cannot silently mis-map features at inference.
FEATURE_NAMES: tuple[str, ...] = (
    "char_length",
    "approx_tokens",
    "has_code_fence",
    "sentence_count",
    "question_marks",
    "complex_keyword_hits",
    "code_keyword_hits",
    "simple_keyword_hits",
)

# Deterministic label encoding, cheapest tier first, so a confusion matrix
# reads in cost order.
TIER_ORDER: tuple[TierName, ...] = (TierName.LOCAL, TierName.HAIKU, TierName.SONNET)
LABEL_TO_INDEX = {tier.value: i for i, tier in enumerate(TIER_ORDER)}
INDEX_TO_LABEL = {i: tier.value for i, tier in enumerate(TIER_ORDER)}


@dataclass(frozen=True)
class LabeledPrompt:
    prompt: str
    label: TierName
    source: str = "outcome"
    note: str = ""


def features_to_vector(f: Features) -> list[float]:
    raw = f.as_dict()
    return [float(raw[name]) for name in FEATURE_NAMES]


def prompt_to_vector(prompt: str) -> list[float]:
    return features_to_vector(extract(prompt))


def write_dataset(rows: list[LabeledPrompt], path: Path = LABELED_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["prompt", "label", "source", "note"])
        for row in rows:
            writer.writerow([row.prompt, row.label.value, row.source, row.note])
    return path


def read_dataset(path: Path = LABELED_PATH) -> list[LabeledPrompt]:
    if not path.exists():
        raise FileNotFoundError(
            f"No labeled dataset at {path}. Build one with "
            "`python -m app.classifier.label` (needs ANTHROPIC_API_KEY), or "
            "`--synthetic` to exercise the pipeline without spending anything."
        )
    with path.open(newline="", encoding="utf-8") as fh:
        return [
            LabeledPrompt(
                prompt=r["prompt"],
                label=TierName(r["label"]),
                source=r.get("source", ""),
                note=r.get("note", ""),
            )
            for r in csv.DictReader(fh)
        ]


def to_xy(rows: list[LabeledPrompt]) -> tuple[list[list[float]], list[int]]:
    x = [prompt_to_vector(r.prompt) for r in rows]
    y = [LABEL_TO_INDEX[r.label.value] for r in rows]
    return x, y
