"""Build the labeled dataset by outcome.

Runs every prompt through every tier, judges which answers are acceptable, and
labels the prompt with the *cheapest tier that produced an acceptable answer*.

Why not label by intuition: a model trained on "this prompt looks hard" learns
only to reproduce the intuition already encoded in the rule classifier, so
comparing the two would measure nothing. Outcome labels describe where the real
capability boundary sits, which is a different and more useful thing.

    python -m app.classifier.label --synthetic     # free, mechanical check only
    python -m app.classifier.label --dry-run       # cost estimate, no calls
    python -m app.classifier.label --limit 20      # cheap real subset
    python -m app.classifier.label                 # the full run (~$3)

Progress is written after every prompt, so an interrupted run resumes instead of
re-spending. Re-running skips prompts already present in the output.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
from pathlib import Path

from app.classifier.corpus import build_corpus
from app.classifier.dataset import LABELED_PATH, LabeledPrompt, read_dataset, write_dataset
from app.config import get_settings
from app.tiers.base import PRICING, TierName
from app.tiers.registry import build_registry

JUDGE_MODEL = "claude-sonnet-5"

JUDGE_PROMPT = """You are grading answers from three AI models of increasing \
capability and cost.

QUESTION:
{prompt}

ANSWER A (smallest model):
{local}

ANSWER B (mid model):
{haiku}

ANSWER C (largest model):
{sonnet}

For each answer, decide whether it is ACCEPTABLE: factually correct, responsive \
to the question, and complete enough to be useful. Judge only quality, not \
length or style.

Reply with exactly three lines and nothing else:
A: ACCEPTABLE or UNACCEPTABLE
B: ACCEPTABLE or UNACCEPTABLE
C: ACCEPTABLE or UNACCEPTABLE"""


def estimate_cost(n: int) -> float:
    """Rough projected spend, so the bill is never a surprise."""
    gen_in, gen_out = 200, 400
    haiku = (gen_in * PRICING[TierName.HAIKU][0] + gen_out * PRICING[TierName.HAIKU][1]) / 1e6
    sonnet = (gen_in * PRICING[TierName.SONNET][0] + gen_out * PRICING[TierName.SONNET][1]) / 1e6
    judge = (1200 * PRICING[TierName.SONNET][0] + 40 * PRICING[TierName.SONNET][1]) / 1e6
    return n * (haiku + sonnet + judge)


def synthetic_labels(prompts: list[str], seed: int = 7) -> list[LabeledPrompt]:
    """Plausible labels with noise, for exercising the pipeline at zero cost.

    These are NOT real labels and are stamped source=synthetic so they can never
    be mistaken for the outcome-based run. Noise is deliberate: without it the
    labels would be a pure function of the features and every model would score
    a perfect, meaningless 100%.
    """
    rng = random.Random(seed)
    rows: list[LabeledPrompt] = []
    for prompt in prompts:
        lowered = prompt.lower()
        if "```" in prompt or "prove" in lowered or "critique" in lowered:
            label = TierName.SONNET
        elif any(k in lowered for k in ("write a", "implement", "regex", "sql", "debug")):
            label = TierName.HAIKU
        else:
            label = TierName.LOCAL
        if rng.random() < 0.15:  # label noise
            label = rng.choice(list(TierName))
        rows.append(LabeledPrompt(prompt, label, source="synthetic", note="NOT REAL"))
    return rows


async def judge(client, prompt: str, answers: dict[TierName, str]) -> dict[TierName, bool]:
    message = await client.messages.create(
        model=JUDGE_MODEL,
        max_tokens=64,
        messages=[
            {
                "role": "user",
                "content": JUDGE_PROMPT.format(
                    prompt=prompt,
                    local=answers[TierName.LOCAL][:2000],
                    haiku=answers[TierName.HAIKU][:2000],
                    sonnet=answers[TierName.SONNET][:2000],
                ),
            }
        ],
    )
    text = "".join(b.text for b in message.content if getattr(b, "type", None) == "text")
    verdicts: dict[TierName, bool] = {}
    for line, tier in zip(text.strip().splitlines(), TIER_SEQUENCE, strict=False):
        verdicts[tier] = "UNACCEPTABLE" not in line.upper()
    # A tier the judge did not rule on is treated as unacceptable: assuming
    # success on a missing verdict would bias labels toward the cheap tier.
    return {tier: verdicts.get(tier, False) for tier in TIER_SEQUENCE}


TIER_SEQUENCE = (TierName.LOCAL, TierName.HAIKU, TierName.SONNET)


async def label_prompts(prompts: list[str], out: Path) -> list[LabeledPrompt]:
    settings = get_settings()
    if settings.mock_tiers:
        sys.exit("Set MOCK_TIERS=false: mock answers are canned and cannot be judged.")
    if not settings.anthropic_api_key:
        sys.exit("ANTHROPIC_API_KEY is required for a real labeling run.")

    from anthropic import AsyncAnthropic

    registry = build_registry(settings)
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)

    done = {r.prompt for r in read_dataset(out)} if out.exists() else set()
    rows = list(read_dataset(out)) if out.exists() else []
    todo = [p for p in prompts if p not in done]
    print(f"{len(done)} already labeled, {len(todo)} to go")

    try:
        for i, prompt in enumerate(todo, 1):
            answers: dict[TierName, str] = {}
            for tier in TIER_SEQUENCE:
                try:
                    answers[tier] = (await registry.get(tier).complete(prompt)).text
                except Exception as exc:  # noqa: BLE001 - one bad tier must not end the run
                    answers[tier] = ""
                    print(f"  [{i}] {tier.value} failed: {exc}")

            verdicts = await judge(client, prompt, answers)
            cheapest = next((t for t in TIER_SEQUENCE if verdicts[t]), None)
            if cheapest is None:
                print(f"  [{i}] no tier acceptable, skipping: {prompt[:60]}")
                continue
            rows.append(LabeledPrompt(prompt, cheapest, source="outcome"))
            write_dataset(rows, out)  # checkpoint every prompt
            print(f"  [{i}/{len(todo)}] {cheapest.value:<7} {prompt[:60]}")
    finally:
        await registry.aclose()
        await client.close()
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--size", type=int, default=300)
    parser.add_argument("--limit", type=int, default=None, help="Label only the first N.")
    parser.add_argument("--synthetic", action="store_true", help="Fake labels, no API calls.")
    parser.add_argument("--dry-run", action="store_true", help="Estimate cost and exit.")
    parser.add_argument("--out", type=Path, default=LABELED_PATH)
    args = parser.parse_args()

    prompts = build_corpus(args.size)
    if args.limit:
        prompts = prompts[: args.limit]

    if args.dry_run:
        print(f"{len(prompts)} prompts -> estimated ${estimate_cost(len(prompts)):.2f}")
        print("Generation on haiku + sonnet (local is free), plus one judge call each.")
        return

    if args.synthetic:
        rows = synthetic_labels(prompts)
        path = write_dataset(rows, args.out)
        print(f"Wrote {len(rows)} SYNTHETIC rows to {path}")
        print("These are not real labels. Re-run without --synthetic before trusting a model.")
        return

    asyncio.run(label_prompts(prompts, args.out))


if __name__ == "__main__":
    main()
