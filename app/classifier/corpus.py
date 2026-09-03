"""Prompt corpus for the labeling run.

Prompts are generated from templates across difficulty bands. Generating the
*prompts* is fine -- what must not be invented is the *labels*, which come from
running each prompt through every tier and comparing the answers. A corpus that
only contained prompts the rule classifier already handles well would make the
comparison meaningless, so the bands deliberately overlap in surface form.
"""

from __future__ import annotations

import random

SIMPLE = [
    "What is the capital of {country}?",
    "Who is {person}?",
    "Define {concept}.",
    "What is {concept}?",
    "When did {event} happen?",
    "Translate '{phrase}' into {language}.",
    "What year did {event} take place?",
    "Who invented {invention}?",
    "What does {acronym} stand for?",
    "Summarize {event} in one sentence.",
]

MODERATE = [
    "Summarize the causes of {event} in a short paragraph.",
    "Write a function that reverses a {structure}.",
    "Implement a helper that validates {validatable}.",
    "Debug this: iterating a {structure} returns None instead of a value.",
    "Write a regex that matches {matchable}.",
    "Explain the difference between {concept} and {concept2}.",
    "What are the trade-offs of using a {structure} here?",
    "Write a unit test for a function that sorts {collection}.",
    "Implement {operation} over a {structure}.",
    "Write a SQL query that finds duplicate {collection}.",
]

HARD = [
    "Prove that {claim}, and explain why each step follows.",
    "Analyze the trade-offs of a {structure} versus {structure2} for a "
    "high-throughput system, and critique the usual recommendation.",
    "Design a {structure} that handles {workload}, then explain why you "
    "rejected the obvious alternative, step by step.",
    "Derive {claim} from first principles and identify where the standard argument is hand-waved.",
    "```python\ndef {function}({param}):\n    pass\n```\nImplement this and "
    "explain the complexity, step by step.",
    "Critique this architecture: a {structure} fronting {structure2}. What "
    "breaks under {workload}, and why?",
    "Explain why {claim}, then analyze what changes if the input is adversarial.",
    "Design a retry policy for {workload} and prove it cannot amplify load.",
]

FILL = {
    "country": [
        "France",
        "Japan",
        "Peru",
        "Kenya",
        "Norway",
        "Chile",
        "Nepal",
        "Portugal",
        "Vietnam",
        "Morocco",
        "Iceland",
        "Uruguay",
    ],
    "person": [
        "Ada Lovelace",
        "Alan Turing",
        "Grace Hopper",
        "Edsger Dijkstra",
        "Barbara Liskov",
        "Claude Shannon",
        "Katherine Johnson",
    ],
    "concept": [
        "entropy",
        "idempotence",
        "backpressure",
        "a memory barrier",
        "eventual consistency",
        "tail latency",
        "a race condition",
        "referential transparency",
        "cache coherence",
    ],
    "concept2": [
        "latency",
        "throughput",
        "strong consistency",
        "a deadlock",
        "head-of-line blocking",
        "amortized cost",
    ],
    "event": [
        "the moon landing",
        "the fall of the Berlin Wall",
        "the invention of the transistor",
        "the first web page",
        "the Bletchley Park effort",
        "the ARPANET rollout",
    ],
    "invention": [
        "the transistor",
        "the compiler",
        "the mouse",
        "TCP/IP",
        "the relational database",
    ],
    "acronym": ["HTTP", "ACID", "CRDT", "RAII", "JIT", "TLS", "RPC"],
    "language": ["Spanish", "German", "Japanese", "Portuguese", "Italian"],
    "phrase": ["good morning", "thank you", "see you tomorrow", "where is the station"],
    "structure": [
        "linked list",
        "hash map",
        "B-tree",
        "ring buffer",
        "priority queue",
        "trie",
        "skip list",
        "bloom filter",
    ],
    "structure2": [
        "a red-black tree",
        "an LSM tree",
        "a hash index",
        "a sorted array",
        "a radix tree",
    ],
    "validatable": [
        "an email address",
        "a UUID",
        "a phone number",
        "an ISO timestamp",
        "a semantic version",
    ],
    "matchable": ["an IPv4 address", "a hex colour", "a UUID", "a quoted string", "an ISO date"],
    "collection": ["rows", "user records", "log entries", "orders", "sessions"],
    "operation": [
        "a breadth-first traversal",
        "an in-order walk",
        "a range query",
        "a bulk insert",
    ],
    "function": ["solve", "merge_ranges", "schedule", "compact", "rebalance"],
    "param": ["items", "nums", "graph", "intervals", "nodes"],
    "workload": [
        "duplicate keys",
        "concurrent writers",
        "bursty traffic",
        "hot partitions",
        "slow consumers",
        "partial failures",
    ],
    "claim": [
        "the halting problem is undecidable",
        "quicksort is O(n log n) on average",
        "no comparison sort beats O(n log n)",
        "two-phase commit blocks on coordinator failure",
        "a bloom filter has no false negatives",
    ],
}


def _fill(template: str, rng: random.Random) -> str:
    out = template
    for key, options in FILL.items():
        token = "{" + key + "}"
        while token in out:
            out = out.replace(token, rng.choice(options), 1)
    return out


def build_corpus(size: int = 300, seed: int = 20260902) -> list[str]:
    """Deterministic corpus, so a re-run labels the same prompts."""
    rng = random.Random(seed)
    bands = [(SIMPLE, 0.40), (MODERATE, 0.35), (HARD, 0.25)]
    prompts: list[str] = []
    seen: set[str] = set()
    guard = 0
    while len(prompts) < size and guard < size * 200:
        guard += 1
        templates = rng.choices([b[0] for b in bands], weights=[b[1] for b in bands], k=1)[0]
        candidate = _fill(rng.choice(templates), rng)
        if candidate not in seen:
            seen.add(candidate)
            prompts.append(candidate)
    if len(prompts) < size:
        raise RuntimeError(
            f"Corpus exhausted at {len(prompts)} of {size} unique prompts; "
            "add templates or fill values."
        )
    return prompts
