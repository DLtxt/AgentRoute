"""Inspect a labeling run before trusting it.

Answers the question a dataset builder cannot answer for you: does this look
right? Reports class balance, how separable the features are, and -- when the
audit trail is present -- the actual answers and judge verdicts behind each
label, so a suspicious one can be checked rather than assumed.

    python -m app.classifier.inspect                  # summary
    python -m app.classifier.inspect --samples 5      # with example rows
    python -m app.classifier.inspect --show 3         # full evidence for row 3
    python -m app.classifier.inspect --label sonnet   # only rows labeled sonnet
    python -m app.classifier.inspect --conflicts      # identical features, different labels
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

from app.classifier.dataset import LABELED_PATH, prompt_to_vector, read_dataset
from app.classifier.label import AUDIT_SUFFIX


def load_audit(path: Path) -> dict[str, dict]:
    audit_path = path.with_suffix(path.suffix + AUDIT_SUFFIX)
    if not audit_path.exists():
        return {}
    records = {}
    for line in audit_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            record = json.loads(line)
            records[record["prompt"]] = record
    return records


def summarize(rows, audit) -> None:
    n = len(rows)
    dist = Counter(r.label.value for r in rows)
    print(f"\n{n} labeled prompts   sources: {dict(Counter(r.source for r in rows))}")
    if audit:
        print(f"audit trail present for {len(audit)}/{n}")
    else:
        print("no audit trail (labeled before evidence capture, or --synthetic)")

    print("\nclass balance")
    for label, count in dist.most_common():
        bar = "#" * round(40 * count / n)
        print(f"  {label:<7} {count:>4}  {count / n:6.1%}  {bar}")
    baseline = dist.most_common(1)[0]
    print(f"  a model must beat {baseline[1] / n:.3f} (always '{baseline[0]}') to be useful")
    if len(dist) < 3:
        print("  WARNING: not all three tiers appear; the missing class cannot be learned")
    elif min(dist.values()) / n < 0.10:
        rarest = min(dist, key=dist.get)
        print(f"  WARNING: '{rarest}' is under 10% -- likely too rare to learn")

    groups = defaultdict(list)
    for r in rows:
        groups[tuple(prompt_to_vector(r.prompt))].append(r.label.value)
    conflicted = sum(1 for r in rows if len(set(groups[tuple(prompt_to_vector(r.prompt))])) > 1)
    ceiling = sum(max(Counter(v).values()) for v in groups.values()) / n
    print("\nseparability")
    print(f"  distinct feature vectors  {len(groups)} for {n} rows")
    print(f"  conflicting rows          {conflicted} ({conflicted / n:.0%})")
    print(f"  achievable ceiling        {ceiling:.3f}")
    print(f"  learnable signal          {ceiling - baseline[1] / n:+.3f}")
    if conflicted / n > 0.25:
        print("  WARNING: many rows share features but disagree on the label")


def show_conflicts(rows, limit: int) -> None:
    groups = defaultdict(list)
    for r in rows:
        groups[tuple(prompt_to_vector(r.prompt))].append(r)
    conflicts = [v for v in groups.values() if len({x.label for x in v}) > 1]
    print(f"\n{len(conflicts)} groups of identical features with disagreeing labels")
    for group in conflicts[:limit]:
        print(f"\n  {dict(Counter(x.label.value for x in group))}")
        for row in group:
            print(f"    {row.label.value:<7} {row.prompt[:78]}")


def show_samples(rows, audit, per_label: int) -> None:
    by_label = defaultdict(list)
    for i, r in enumerate(rows):
        by_label[r.label.value].append((i, r))
    for label, items in sorted(by_label.items()):
        print(f"\n--- labeled '{label}' ({len(items)} rows) ---")
        for index, row in items[:per_label]:
            print(f"  [{index}] {row.prompt[:88]}")
            record = audit.get(row.prompt)
            if record:
                v = record["verdicts"]
                flags = "  ".join(
                    f"{k}={'OK' if v[k] else 'no'}" for k in ("local", "haiku", "sonnet")
                )
                print(f"        verdicts: {flags}")


def show_one(rows, audit, index: int) -> None:
    if not 0 <= index < len(rows):
        print(f"index {index} out of range (0..{len(rows) - 1})")
        return
    row = rows[index]
    print(f"\n[{index}] {row.prompt}")
    print(f"label: {row.label.value}")
    record = audit.get(row.prompt)
    if not record:
        print("\nNo audit record for this prompt -- rerun the labeling to capture evidence.")
        return
    print(f"\njudge said:\n{record['judge_raw']}")
    answers = record["answers"]
    for i, text in enumerate(answers["local"], 1):
        print(f"\n--- local sample {i} ---\n{text[:900]}")
    for tier in ("haiku", "sonnet"):
        print(f"\n--- {tier} ---\n{answers[tier][:900]}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=LABELED_PATH)
    parser.add_argument("--samples", type=int, default=0, help="Example rows per label.")
    parser.add_argument("--show", type=int, default=None, help="Full evidence for one row index.")
    parser.add_argument("--label", type=str, default=None, help="Filter to one label.")
    parser.add_argument("--conflicts", action="store_true", help="Show disagreeing groups.")
    args = parser.parse_args()

    rows = read_dataset(args.data)
    audit = load_audit(args.data)

    if args.show is not None:
        show_one(rows, audit, args.show)
        return

    if args.label:
        rows = [r for r in rows if r.label.value == args.label]
        print(f"filtered to {len(rows)} rows labeled '{args.label}'")
        for i, r in enumerate(rows):
            print(f"  [{i}] {r.prompt[:92]}")
        return

    summarize(rows, audit)
    if args.conflicts:
        show_conflicts(rows, limit=8)
    if args.samples:
        show_samples(rows, audit, args.samples)


if __name__ == "__main__":
    main()
