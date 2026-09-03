"""Train the tier classifiers: logistic regression, then XGBoost.

Both models see identical hand-crafted features -- not embeddings -- so a
feature-importance chart can explain exactly why a prompt routed where it did.

The logistic regression is not a formality. It is the baseline that makes
choosing XGBoost defensible rather than decorative: if the simpler model wins,
that is the result, and saying so is a better answer than a rationalisation.

    python -m app.classifier.train
"""

from __future__ import annotations

import argparse
import json
import pickle
from collections import Counter
from pathlib import Path

from app.classifier.dataset import (
    FEATURE_NAMES,
    INDEX_TO_LABEL,
    LABELED_PATH,
    read_dataset,
    to_xy,
)

MODEL_DIR = Path(__file__).resolve().parents[2] / "models"


def _confusion(y_true: list[int], y_pred: list[int], n: int) -> list[list[int]]:
    m = [[0] * n for _ in range(n)]
    for t, p in zip(y_true, y_pred, strict=True):
        m[t][p] += 1
    return m


def _render(matrix: list[list[int]]) -> str:
    labels = [INDEX_TO_LABEL[i] for i in range(len(matrix))]
    head = "            " + "".join(f"{lab:>9}" for lab in labels) + "   (predicted)"
    lines = [head]
    for i, row in enumerate(matrix):
        lines.append(f"  {labels[i]:>9} " + "".join(f"{v:>9}" for v in row))
    lines.append("  (actual)")
    return "\n".join(lines)


def _data_quality(x: list[list[float]], y: list[int]) -> dict:
    """Three checks that decide whether training can possibly work.

    A model cannot beat the majority-class rate without learning something, and
    it cannot separate rows whose feature vectors are identical but whose labels
    differ. Reporting both up front turns "0.685, not great" into "0.685, which
    is worse than guessing" -- the difference between a mediocre result and a
    failed one.
    """
    counts: dict[int, int] = {}
    for label in y:
        counts[label] = counts.get(label, 0) + 1
    majority = max(counts.values()) / len(y)

    groups: dict[tuple, list[int]] = {}
    for vector, label in zip(x, y, strict=True):
        groups.setdefault(tuple(vector), []).append(label)
    conflicted = sum(1 for vector in x if len(set(groups[tuple(vector)])) > 1)

    # Best possible accuracy on these features: for each set of identical
    # vectors, always predict its most common label. The gap between this and
    # the baseline is the entire amount of signal available to learn.
    ceiling = sum(max(Counter(labels).values()) for labels in groups.values()) / len(y)

    print("\ndata quality")
    for index, count in sorted(counts.items()):
        print(f"  {INDEX_TO_LABEL[index]:<8} {count:>4}  {count / len(y):6.1%}")
    print(f"  majority-class baseline      {majority:.3f}")
    print(f"  ceiling on these features    {ceiling:.3f}  (in-sample, optimistic)")
    print(f"  learnable signal             {ceiling - majority:+.3f}")
    print("  rows with identical features")
    print(f"    but conflicting labels     {conflicted}/{len(y)} ({conflicted / len(y):.0%})")
    if conflicted / len(y) > 0.1:
        print("  ^ those rows are unlearnable by construction; no model can")
        print("    separate them, and they cap the achievable accuracy.")
    return {
        "majority_baseline": majority,
        "feature_ceiling": ceiling,
        "unlearnable_rows": conflicted,
    }


def _report(name: str, y_true: list[int], y_pred: list[int]) -> dict:
    n = len(INDEX_TO_LABEL)
    matrix = _confusion(y_true, y_pred, n)
    correct = sum(matrix[i][i] for i in range(n))
    accuracy = correct / len(y_true) if y_true else 0.0
    print(f"\n{name}  accuracy {accuracy:.3f}  ({correct}/{len(y_true)} held out)")
    print(_render(matrix))
    return {"accuracy": accuracy, "confusion": matrix, "n_test": len(y_true)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=LABELED_PATH)
    parser.add_argument("--test-size", type=float, default=0.25)
    parser.add_argument("--seed", type=int, default=20260902)
    parser.add_argument(
        "--balanced",
        action="store_true",
        help="Weight classes inversely to frequency. Stops the model "
        "collapsing onto the majority class, at the cost of raw accuracy.",
    )
    args = parser.parse_args()

    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import train_test_split
    from xgboost import XGBClassifier

    rows = read_dataset(args.data)
    synthetic = sum(1 for r in rows if r.source == "synthetic")
    if synthetic:
        print("=" * 72)
        print(f"WARNING: {synthetic}/{len(rows)} rows are SYNTHETIC.")
        print("Every score below is fitted to invented labels and means nothing")
        print("about real routing quality. It proves the pipeline runs, no more.")
        print("Run `python -m app.classifier.label` for outcome-based labels.")
        print("=" * 72)

    x, y = to_xy(rows)
    quality = _data_quality(x, y)

    x_train, x_test, y_train, y_test = train_test_split(
        x, y, test_size=args.test_size, random_state=args.seed, stratify=y
    )
    print(f"\n{len(x_train)} train / {len(x_test)} test, {len(FEATURE_NAMES)} features")

    metrics: dict[str, dict] = {}

    # Balanced weights, or the minority classes are simply never predicted:
    # with 70% of rows on one label, ignoring the other two scores well.
    logreg = LogisticRegression(max_iter=2000, class_weight="balanced" if args.balanced else None)
    logreg.fit(x_train, y_train)
    metrics["logreg"] = _report("logreg ", y_test, list(logreg.predict(x_test)))

    freq = Counter(y_train)
    weights = (
        [len(y_train) / (len(freq) * freq[label]) for label in y_train] if args.balanced else None
    )
    xgb = XGBClassifier(
        n_estimators=200,
        max_depth=4,
        learning_rate=0.1,
        objective="multi:softmax",
        num_class=len(INDEX_TO_LABEL),
        random_state=args.seed,
        verbosity=0,
    )
    xgb.fit(x_train, y_train, sample_weight=weights)
    metrics["xgb"] = _report("xgboost", y_test, list(xgb.predict(x_test)))

    print("\nxgboost feature importance")
    ranked = sorted(
        zip(FEATURE_NAMES, xgb.feature_importances_, strict=True),
        key=lambda kv: kv[1],
        reverse=True,
    )
    for name, score in ranked:
        print(f"  {name:<22} {score:.4f}")

    MODEL_DIR.mkdir(exist_ok=True)
    for name, model in (("logreg", logreg), ("xgb", xgb)):
        with (MODEL_DIR / f"{name}.pkl").open("wb") as fh:
            # Feature names travel with the model so a reordering in
            # dataset.py cannot silently mis-map inputs at inference time.
            pickle.dump({"model": model, "features": FEATURE_NAMES}, fh)

    (MODEL_DIR / "metrics.json").write_text(
        json.dumps(
            {
                "n_train": len(x_train),
                "n_test": len(x_test),
                "synthetic_rows": synthetic,
                "trustworthy": synthetic == 0,
                **quality,
                **metrics,
            },
            indent=2,
        )
    )

    delta = metrics["xgb"]["accuracy"] - metrics["logreg"]["accuracy"]
    baseline = quality["majority_baseline"]
    print(f"\nxgboost - logreg: {delta:+.3f} on {len(x_test)} held-out samples.")
    if abs(delta) < 0.05:
        print("Within noise at this sample size -- do not claim a winner.")

    print(
        f"\nversus the majority-class baseline ({baseline:.3f}), "
        f"ceiling {quality['feature_ceiling']:.3f}:"
    )
    for name, m in metrics.items():
        gap = m["accuracy"] - baseline
        verdict = "beats it" if gap > 0.02 else "NO BETTER THAN GUESSING"
        print(f"  {name:<8} {m['accuracy']:.3f}  {gap:+.3f}  {verdict}")
    if all(m["accuracy"] <= baseline + 0.02 for m in metrics.values()):
        print("\nNeither model beats a constant prediction. The dataset, not the")
        print("model choice, is the thing to fix -- check the class balance and")
        print("the unlearnable-row count above.")
    print(f"Saved to {MODEL_DIR}")


if __name__ == "__main__":
    main()
