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
import json
import random
import re
import sys
from dataclasses import replace
from pathlib import Path

from app.classifier.corpus import build_corpus
from app.classifier.dataset import LABELED_PATH, LabeledPrompt, read_dataset, write_dataset
from app.config import get_settings
from app.tiers.base import PRICING, TierName
from app.tiers.registry import build_registry


class JudgeUnusable(RuntimeError):
    """The grader returned nothing usable -- a tooling failure, not a verdict."""


JUDGE_MODEL = "claude-sonnet-5"

# Answers are capped by max_tokens, and a truncated answer ends mid-sentence and
# reads as wrong to any grader -- which is how the first real run came to skip
# most of its prompts as "no tier acceptable" when all three tiers had in fact
# answered well. Two changes prevent that: ask for a short answer, so responses
# finish naturally well inside the budget, and give a budget large enough that
# they do. The instruction is identical for every tier, so it constrains them
# equally and the comparison stays fair.
ANSWER_INSTRUCTION = (
    "Answer in at most 150 words. Be complete but concise; do not pad with examples or caveats.\n\n"
)
# Generous because Sonnet's own adaptive thinking also draws on this budget.
# Thinking stays ON for the answering tiers -- it is part of what makes Sonnet
# the capable tier, and switching it off would understate the very capability
# the labels are meant to measure. The concision instruction, not a tight cap,
# is what keeps answers short.
LABEL_MAX_TOKENS = 1500

# How much of each answer the judge sees. Generous, because clipping an answer
# before grading recreates the same truncation problem one layer up.
JUDGE_CLIP_CHARS = 6000

# Sonnet 5 runs adaptive thinking unless told otherwise, and thinking tokens
# count against max_tokens. Grading three real answers is enough work that the
# thinking consumed the entire budget and the response came back with NO text
# at all -- which the parser then read as "every tier unacceptable", silently
# skipping most of the run. Two reasons to switch it off here rather than just
# raise the budget: the task is a three-line classification that does not need
# extended reasoning, and at ~1500 thinking tokens per call across 300 prompts
# it would have cost more than the answers being graded.
AUDIT_SUFFIX = ".audit.jsonl"
JUDGE_MAX_TOKENS = 256
JUDGE_THINKING = {"type": "disabled"}

# The smallest model is sampled several times and graded on a majority vote.
# Measured: relabelling one prompt three times gave [haiku, haiku, local] --
# the same prompt, different label, because the small model's answer quality is
# genuinely borderline rather than because the grader is inconsistent. Voting
# turns a coin flip into a stable label. It is free: the local tier is Ollama.
LOCAL_SAMPLES = 3

JUDGE_PROMPT = """You are grading answers from three AI models of increasing \
capability and cost. The smallest model was asked the question several times, \
so its answers appear as A1, A2, A3.

QUESTION:
{prompt}

ANSWER A1 (smallest model):
{local1}

ANSWER A2 (smallest model):
{local2}

ANSWER A3 (smallest model):
{local3}

ANSWER B (mid model):
{haiku}

ANSWER C (largest model):
{sonnet}

For each answer, decide whether it is ACCEPTABLE: factually correct, responsive \
to the question, and complete enough to be useful. Judge only quality, not \
length or style. Grade each of A1, A2 and A3 independently.

Reply with exactly five lines and nothing else:
A1: ACCEPTABLE or UNACCEPTABLE
A2: ACCEPTABLE or UNACCEPTABLE
A3: ACCEPTABLE or UNACCEPTABLE
B: ACCEPTABLE or UNACCEPTABLE
C: ACCEPTABLE or UNACCEPTABLE"""


def estimate_cost(n: int) -> float:
    """Rough projected spend, so the bill is never a surprise."""
    # Measured from a sample run: concise answers plus Sonnet's thinking.
    gen_in, gen_out = 250, 550
    haiku = (gen_in * PRICING[TierName.HAIKU][0] + gen_out * PRICING[TierName.HAIKU][1]) / 1e6
    sonnet = (gen_in * PRICING[TierName.SONNET][0] + gen_out * PRICING[TierName.SONNET][1]) / 1e6
    # Judge thinking is disabled, so its output is a few dozen tokens.
    judge = (1800 * PRICING[TierName.SONNET][0] + 40 * PRICING[TierName.SONNET][1]) / 1e6
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


async def judge(
    client, prompt: str, local_answers: list[str], haiku: str, sonnet: str
) -> tuple[dict[TierName, bool], str]:
    padded = (local_answers + ["(no answer)"] * LOCAL_SAMPLES)[:LOCAL_SAMPLES]
    message = await client.messages.create(
        model=JUDGE_MODEL,
        max_tokens=JUDGE_MAX_TOKENS,
        thinking=JUDGE_THINKING,
        messages=[
            {
                "role": "user",
                "content": JUDGE_PROMPT.format(
                    prompt=prompt,
                    local1=padded[0][:JUDGE_CLIP_CHARS],
                    local2=padded[1][:JUDGE_CLIP_CHARS],
                    local3=padded[2][:JUDGE_CLIP_CHARS],
                    haiku=haiku[:JUDGE_CLIP_CHARS],
                    sonnet=sonnet[:JUDGE_CLIP_CHARS],
                ),
            }
        ],
    )
    text = "".join(b.text for b in message.content if getattr(b, "type", None) == "text")
    if not text.strip():
        # Never fail silently here. An empty judge response used to mean every
        # tier was recorded unacceptable and the prompt skipped, which looked
        # like the models failing rather than the grader never answering.
        raise JudgeUnusable(
            f"Judge returned no text (stop_reason={getattr(message, 'stop_reason', None)!r}, "
            f"output_tokens={message.usage.output_tokens}). "
            "Raise JUDGE_MAX_TOKENS or check JUDGE_THINKING."
        )

    def verdict(marker: str) -> bool | None:
        match = re.search(rf"^\s*{marker}\s*[:.\)-]\s*(\w+)", text, re.MULTILINE)
        return match.group(1).upper().startswith("ACCEPT") if match else None

    # Local passes on a majority of its samples, not on a single lucky draw.
    votes = [verdict(f"A{i + 1}") for i in range(LOCAL_SAMPLES)]
    passes = sum(1 for v in votes if v)
    # A marker the judge did not rule on counts as unacceptable: assuming
    # success on a missing verdict would bias labels toward the cheap tier.
    verdicts = {
        TierName.LOCAL: passes * 2 > LOCAL_SAMPLES,
        TierName.HAIKU: bool(verdict("B")),
        TierName.SONNET: bool(verdict("C")),
    }
    return verdicts, text.strip()


TIER_SEQUENCE = (TierName.LOCAL, TierName.HAIKU, TierName.SONNET)


async def generate(
    registry, tier: TierName, prompt: str, index: int, truncations: dict[str, int]
) -> str:
    """One answer from one tier. A failing tier yields an empty string rather
    than ending the run -- one bad tier should not cost the whole dataset."""
    try:
        result = await registry.get(tier).complete(ANSWER_INSTRUCTION + prompt)
        if result.truncated:
            truncations[tier.value] += 1
            print(
                f"  [{index}] WARNING {tier.value} was truncated at "
                f"{result.output_tokens} tokens; its answer will grade badly"
            )
        return result.text
    except Exception as exc:  # noqa: BLE001 - one bad tier must not end the run
        print(f"  [{index}] {tier.value} failed: {exc}")
        return ""


async def label_prompts(prompts: list[str], out: Path) -> list[LabeledPrompt]:
    settings = get_settings()
    if settings.mock_tiers:
        sys.exit("Set MOCK_TIERS=false: mock answers are canned and cannot be judged.")
    if not settings.anthropic_api_key:
        sys.exit("ANTHROPIC_API_KEY is required for a real labeling run.")

    from anthropic import AsyncAnthropic

    # Labeling needs more headroom than the gateway's default, so answers
    # finish rather than being cut off and graded as failures.
    registry = build_registry(replace(settings, max_tokens=LABEL_MAX_TOKENS))
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)

    existing = read_dataset(out) if out.exists() else []
    # Only real labels count as done. Synthetic rows are placeholders written to
    # exercise the pipeline, and treating them as completed work is exactly
    # backwards -- a real run exists to replace them.
    rows = [r for r in existing if r.source == "outcome"]
    discarded = len(existing) - len(rows)
    if discarded:
        print(f"Discarding {discarded} synthetic placeholder rows.")

    audit_path = out.with_suffix(out.suffix + AUDIT_SUFFIX)
    truncations: dict[str, int] = {t.value: 0 for t in TIER_SEQUENCE}
    skipped = 0

    done = {r.prompt for r in rows}
    todo = [p for p in prompts if p not in done]
    print(f"{len(done)} real labels already present, {len(todo)} to go")
    if not todo:
        print("Nothing to do.")
        return rows

    try:
        for i, prompt in enumerate(todo, 1):
            local_answers = [
                await generate(registry, TierName.LOCAL, prompt, i, truncations)
                for _ in range(LOCAL_SAMPLES)
            ]
            haiku_answer = await generate(registry, TierName.HAIKU, prompt, i, truncations)
            sonnet_answer = await generate(registry, TierName.SONNET, prompt, i, truncations)

            try:
                verdicts, raw_verdict = await judge(
                    client, prompt, local_answers, haiku_answer, sonnet_answer
                )
            except JudgeUnusable as exc:
                print(f"\n  [{i}] JUDGE FAILED: {exc}")
                print("  Stopping: continuing would record made-up labels.")
                break
            cheapest = next((t for t in TIER_SEQUENCE if verdicts[t]), None)
            if cheapest is None:
                skipped += 1
                print(f"  [{i}] no tier acceptable, skipping: {prompt[:60]}")
                continue
            rows.append(LabeledPrompt(prompt, cheapest, source="outcome"))
            write_dataset(rows, out)  # checkpoint every prompt

            # Append the evidence behind this label so it can be audited.
            with audit_path.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "prompt": prompt,
                            "label": cheapest.value,
                            "verdicts": {t.value: verdicts[t] for t in TIER_SEQUENCE},
                            "judge_raw": raw_verdict,
                            "answers": {
                                "local": local_answers,
                                "haiku": haiku_answer,
                                "sonnet": sonnet_answer,
                            },
                        }
                    )
                    + "\n"
                )
            print(f"  [{i}/{len(todo)}] {cheapest.value:<7} {prompt[:60]}")
    finally:
        await registry.aclose()
        await client.close()
        if skipped:
            print(f"\nSkipped {skipped} prompts with no acceptable answer.")
        print(f"Evidence for every label written to {audit_path}")
        print("Inspect it with: python -m app.classifier.inspect")
        if any(truncations.values()):
            print(f"Truncated answers by tier: {truncations}")
            print(
                "Truncation makes an answer look wrong to the judge. Raise "
                "LABEL_MAX_TOKENS if this count is not near zero."
            )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--size", type=int, default=300)
    parser.add_argument("--limit", type=int, default=None, help="Label only the first N.")
    parser.add_argument("--synthetic", action="store_true", help="Fake labels, no API calls.")
    parser.add_argument("--dry-run", action="store_true", help="Estimate cost and exit.")
    parser.add_argument("--out", type=Path, default=LABELED_PATH)
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Discard existing labels and start over, instead of resuming.",
    )
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

    if args.fresh and args.out.exists():
        args.out.unlink()
        print(f"Removed {args.out}; starting fresh.")
    asyncio.run(label_prompts(prompts, args.out))


if __name__ == "__main__":
    main()
