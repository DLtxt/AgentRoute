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
    x_train, x_test, y_train, y_test = train_test_split(
        x, y, test_size=args.test_size, random_state=args.seed, stratify=y
    )
    print(f"\n{len(x_train)} train / {len(x_test)} test, {len(FEATURE_NAMES)} features")

    metrics: dict[str, dict] = {}

    logreg = LogisticRegression(max_iter=2000)
    logreg.fit(x_train, y_train)
    metrics["logreg"] = _report("logreg ", y_test, list(logreg.predict(x_test)))

    xgb = XGBClassifier(
        n_estimators=200,
        max_depth=4,
        learning_rate=0.1,
        objective="multi:softmax",
        num_class=len(INDEX_TO_LABEL),
        random_state=args.seed,
        verbosity=0,
    )
    xgb.fit(x_train, y_train)
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
                **metrics,
            },
            indent=2,
        )
    )

    delta = metrics["xgb"]["accuracy"] - metrics["logreg"]["accuracy"]
    print(f"\nxgboost - logreg: {delta:+.3f} on {len(x_test)} held-out samples.")
    if abs(delta) < 0.05:
        print("Within noise at this sample size -- do not claim a winner.")
    print(f"Saved to {MODEL_DIR}")


if __name__ == "__main__":
    main()
