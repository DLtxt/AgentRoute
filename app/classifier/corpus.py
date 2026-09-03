"""Prompt corpus for the labeling run.

Curated rather than generated. An earlier version filled slots in templates,
which produced prompts differing by a single noun -- 69% of them shared an
identical feature vector with another prompt, so no classifier could separate
them however they were labeled.

These prompts vary in length, sentence count, phrasing, and structure as well as
in difficulty, which is what gives the feature extractor something to work with.
"""

from __future__ import annotations

SIMPLE = [
    "What is the capital of Portugal?",
    "Who wrote the first compiler?",
    "Define idempotence.",
    "What does ACID stand for?",
    "Translate 'good morning' into Spanish.",
    "What year did the first web page go live?",
    "Name three sorting algorithms.",
    "What is a UUID?",
    "Is HTTP stateless?",
    "What port does HTTPS use by default?",
    "Give me a one-sentence summary of the moon landing.",
    "What is the difference between a list and a tuple in Python?",
    "Spell 'necessary'.",
    "What is 17 times 23?",
    "Who was Ada Lovelace?",
    "What does DNS do?",
    "Convert 100 Fahrenheit to Celsius.",
    "What is the largest planet in the solar system?",
    "List the days of the week in French.",
    "What is the boiling point of water at sea level?",
    "Abbreviate 'representational state transfer'.",
    "What language is Redis written in?",
    "When was the transistor invented?",
    "What is a palindrome?",
    "Give an example of a prime number over 100.",
    "What does the acronym SQL stand for?",
    "Who founded the Linux kernel project?",
    "What is the chemical symbol for gold?",
    "How many bytes are in a kilobyte?",
    "Name the four cardinal directions.",
    "What is the plural of 'index'?",
    "What does TCP stand for?",
    "Translate 'thank you' into Japanese.",
    "What is the capital of Kenya?",
    "Define latency in one sentence.",
    "What is the speed of light in a vacuum?",
    "Who painted the Mona Lisa?",
    "What is 2 to the power of 10?",
    "What is the currency of Norway?",
    "Name a NoSQL database.",
]

MODERATE = [
    "Write a Python function that reverses a singly linked list in place.",
    "Explain the difference between a process and a thread, with an example of when each is preferable.",
    "Write a SQL query that returns customers who placed more than three orders last month.",
    "Implement a function that validates an email address, and say what your validation deliberately does not catch.",
    "Debug this description: a recursive tree walk returns None for every leaf. What are the two likeliest causes?",
    "Write a regex matching ISO 8601 dates, and explain each group.",
    "Summarize the main causes of the 2008 financial crisis in about a paragraph.",
    "What are the trade-offs between a hash map and a balanced tree for an in-memory index?",
    "Write a unit test for a function that merges overlapping intervals. Cover the edge cases you think matter.",
    "Explain what a database transaction isolation level is, and describe two of them.",
    "Convert this to a list comprehension: a loop that squares even numbers from a list.",
    "Implement binary search over a sorted array and state its preconditions.",
    "Explain why floating point arithmetic gives 0.1 + 0.2 != 0.3.",
    "Write a function that flattens an arbitrarily nested list without recursion.",
    "Describe how a bloom filter works and what guarantee it does and does not give you.",
    "Write a shell one-liner that finds the ten largest files under a directory.",
    "Explain the difference between authentication and authorization with a concrete example.",
    "Implement a least-recently-used cache with O(1) get and put.",
    "What is the difference between a 301 and a 302 redirect, and when does it matter?",
    "Write a function that detects a cycle in a linked list, and explain the space complexity.",
    "Explain what happens, step by step, when you type a URL into a browser and press enter.",
    "Write a SQL query that finds duplicate rows by email, keeping the earliest by created_at.",
    "Describe the difference between optimistic and pessimistic locking.",
    "Implement a rate limiter using a token bucket, and describe its burst behaviour.",
    "Explain what an index does to write performance, not just read performance.",
    "Write a function that parses a CSV line containing quoted commas.",
    "What is the difference between horizontal and vertical scaling? Give a case where vertical is the better choice.",
    "Explain memoization and show it applied to a Fibonacci function.",
    "Write a test that verifies a retry policy backs off exponentially.",
    "Describe how garbage collection works in a generational collector.",
    "Explain the CAP theorem and name a system that chooses each pair.",
    "Implement a function that returns the k most frequent elements in a list.",
    "What does 'eventually consistent' actually promise a client?",
    "Write a script that rotates log files older than seven days.",
    "Explain the difference between a semaphore and a mutex.",
    "Implement a debounce function and explain where it differs from throttle.",
    "Describe what a connection pool solves and how it can itself become a bottleneck.",
    "Write a query plan explanation for why a LIKE '%foo' cannot use a normal index.",
    "Explain what makes a hash function suitable for cryptography rather than for a hash table.",
    "Implement a function that merges two sorted iterators lazily.",
    "Explain what happens to an in-flight HTTP request when the server process receives SIGTERM.",
    "Write a function that groups a list of records by a key and returns the counts, sorted descending.",
    "Describe the difference between a symlink and a hard link, and when each breaks.",
    "Implement exponential backoff with jitter and explain why the jitter matters.",
    "Explain what an ORM's N+1 query problem is and show how you would detect it.",
    "Write a function that safely parses a JSON body that may be malformed, and describe your error contract.",
    "What is the difference between a container image layer and a container, and why does that matter for image size?",
    "Implement a function that chunks an iterable into batches of size n without loading it all into memory.",
    "Explain how DNS resolution works end to end, including caching at each level.",
    "Write a query that computes a running total per customer ordered by date.",
    "Describe how you would migrate a column from nullable to non-null on a live table.",
    "Explain what a race condition is and give an example that a unit test would not catch.",
    "Implement a function that computes the Levenshtein distance between two strings.",
    "Describe the difference between blue-green and canary deployment, and when each is preferable.",
    "Explain what happens when a Kubernetes readiness probe fails, versus a liveness probe.",
]

HARD = [
    "Prove that the halting problem is undecidable, and explain why the diagonal argument is not circular.",
    "Design a distributed rate limiter that stays correct under network partition. Explain what you give up and why that is the right trade.",
    "Critique this architecture: a single Redis instance fronting a sharded Postgres cluster, with the application doing its own two-phase commit across both. What fails first under load, and what would you change?",
    "Derive the average-case O(n log n) bound for quicksort from first principles, and identify where the standard textbook argument hand-waves.",
    "```python\ndef schedule(tasks, workers):\n    pass\n```\nImplement a fair scheduler for this signature, prove it cannot starve a task, and analyze the complexity.",
    "Explain why no comparison sort can beat O(n log n), then describe precisely what radix sort does differently and why it is not a counterexample.",
    "Design a schema migration strategy for a table with two billion rows and no maintenance window. Justify each step and say what you would monitor.",
    "Two-phase commit blocks when the coordinator fails. Prove it, then explain what three-phase commit changes and why it is still not enough under an asynchronous network.",
    "Critique the claim that microservices improve reliability. Steel-man it first, then give the strongest counterargument.",
    "Design an idempotency scheme for a payment API where the client may retry indefinitely and the network may reorder. Explain your key choice and its failure modes.",
    "Analyze the trade-offs between an LSM tree and a B-tree for a write-heavy workload with occasional range scans, and say which you would pick and why.",
    "```sql\nSELECT * FROM orders o JOIN users u ON o.user_id = u.id WHERE u.country = 'NO' ORDER BY o.created_at DESC LIMIT 20;\n```\nThis query is slow on a large table. Walk through how you would diagnose it and what indexes you would consider, with reasoning for each.",
    "Explain why linearizability and serializability are different guarantees, and construct an example history that satisfies one but not the other.",
    "Design a system that deduplicates a stream of ten billion events per day with bounded memory. Justify your data structure and quantify the error you accept.",
    "Prove that a bloom filter has no false negatives, then derive the false positive rate as a function of size and hash count.",
    "Critique this plan: cache invalidation by TTL only, on data that other services can mutate. When is that actually correct, and when is it quietly wrong?",
    "Design a leader election protocol for five nodes over an unreliable network. Explain how you avoid split brain and what assumptions you are relying on.",
    "Explain the difference between at-least-once, at-most-once, and exactly-once delivery, and argue whether exactly-once is achievable end to end.",
    "Given a service whose p99 latency is ten times its p50, walk through how you would find the cause, and explain what each hypothesis would predict.",
    "Design a backfill for a corrupted derived table that must not double-count and must be resumable. Explain your correctness argument.",
    "Explain why a monotonic clock is required for measuring elapsed time, and describe a bug caused by using wall-clock time instead.",
    "Design a feature flag system that can be evaluated client-side without leaking unreleased feature names. Justify the trade-off you make.",
    "Prove that any consistent hashing scheme with virtual nodes bounds the keys remapped on node removal, and state the bound.",
    "Critique this approach to zero-downtime deploys: drain connections for 30 seconds, then hard-kill. Under what traffic shape does it drop requests?",
    "Design a multi-tenant rate limiter where one tenant's burst must not degrade another's latency. Explain your isolation mechanism.",
    "Explain what a write-ahead log buys you that fsync-on-write does not, and describe the failure it still cannot survive.",
    "Given a memory leak that only appears under production traffic, describe your diagnostic sequence and what each step would rule out.",
    "Design an audit log that is append-only and tamper-evident without a blockchain. Explain your integrity argument.",
    "Analyze whether a read-through cache or a write-through cache is safer for a system where reads vastly outnumber writes, and defend your answer.",
    "```go\nfunc Merge(chans []<-chan int) <-chan int {\n\treturn nil\n}\n```\nImplement fan-in for this signature, explain how you avoid a goroutine leak, and prove termination.",
    "Explain why retries without jitter can synchronize clients into a thundering herd, and derive roughly how bad it gets with N clients.",
    "Design a schema for hierarchical data that supports both 'all descendants' and 'all ancestors' queries efficiently. Compare two approaches.",
    "Critique the practice of using UUIDv4 as a clustered primary key at scale, then say when it is nonetheless the right choice.",
    "Explain the difference between a memory barrier and a compiler barrier, and give a concrete bug that needs each.",
    "Design a strategy for testing a distributed system's behaviour under partition without a full chaos-engineering platform.",
    # Prompts demanding a *checkable* artifact. Measured: the mid tier writes
    # convincing prose but produces concrete errors when the answer must
    # compile, the algebra must hold, or the proof must follow from the code it
    # just wrote -- one answer called wg.WaitGroup() instead of wg.Wait(),
    # another derived T(n) = T(n-1) + O(1) and claimed O(n log n) from it.
    # Prose-only hard prompts do not separate the tiers; these do.
    "```python\nclass RateLimiter:\n    def allow(self, key: str, now: float) -> bool:\n        ...\n```\nImplement a sliding-window rate limiter for this signature. Prove the window is exact rather than approximate, and give the memory bound per key.",
    "```python\ndef lru_cache_with_ttl(capacity: int, ttl: float):\n    ...\n```\nImplement this with O(1) get and put including expiry. Prove that an expired entry is never returned, and state what happens when capacity and expiry conflict.",
    "```go\nfunc Pipeline(ctx context.Context, in <-chan int) <-chan int {\n\treturn nil\n}\n```\nImplement a cancellable pipeline stage. Prove no goroutine outlives the context, and explain the exact ordering guarantee on close.",
    "```python\ndef merge_intervals(intervals):\n    ...\n```\nImplement this correctly for touching, nested, and zero-length intervals. Give the invariant your loop maintains and prove it holds at every step.",
    "```sql\n-- orders(id, customer_id, amount, created_at, status)\n```\nWrite a query returning each customer's second-largest completed order, with no window functions. Prove it is correct when a customer has ties or fewer than two orders.",
    "```python\ndef topological_sort(graph):\n    ...\n```\nImplement this so it detects cycles rather than looping forever. Prove your cycle detection is complete, and state the complexity in terms of vertices and edges.",
    "```go\ntype Semaphore struct{}\nfunc (s *Semaphore) Acquire(ctx context.Context) error { return nil }\n```\nImplement a weighted semaphore honouring context cancellation. Prove a cancelled waiter cannot leave the count corrupted.",
    "```python\ndef binary_search_rotated(nums, target):\n    ...\n```\nImplement search on a rotated sorted array with duplicates. Prove the worst case is O(n) rather than O(log n) and identify exactly which input forces it.",
    "Derive the expected number of comparisons for randomized quickselect and prove it is linear, then show precisely where an argument that only bounds the recursion depth is insufficient.",
    "```python\ndef debounce(fn, wait: float):\n    ...\n```\nImplement a thread-safe debounce decorator. Prove that concurrent calls cannot schedule two overlapping invocations, and say what your lock does not protect.",
    "Prove that a lock-free single-producer single-consumer ring buffer is correct with only acquire/release ordering, and identify the exact operation that would break under relaxed ordering.",
    "```python\ndef parse_semver(text: str) -> tuple:\n    ...\n```\nImplement a parser accepting prerelease and build metadata per the specification, rejecting malformed input. Give three inputs a naive regex would wrongly accept.",
    "```go\nfunc Retry(ctx context.Context, attempts int, f func() error) error {\n\treturn nil\n}\n```\nImplement retry with exponential backoff and jitter. Prove total elapsed time is bounded, and explain why the naive implementation can exceed the deadline.",
    "Derive the false-positive rate of a counting Bloom filter with 4-bit counters, and prove where it diverges from the standard Bloom filter bound once counters saturate.",
    "```python\ndef consistent_hash_ring(nodes, replicas: int):\n    ...\n```\nImplement a consistent hash ring with virtual nodes. Prove the fraction of keys remapped when one node leaves, and state the assumption that proof depends on.",
    "```python\ndef reservoir_sample(stream, k: int):\n    ...\n```\nImplement reservoir sampling and prove every element ends up with probability exactly k/n, being explicit about the induction step.",
    "```go\nfunc WorkerPool(jobs <-chan Job, n int) <-chan Result {\n\treturn nil\n}\n```\nImplement a worker pool that propagates panics as errors without killing the pool. Prove no result is lost and no goroutine leaks.",
    "```python\ndef median_of_two_sorted(a, b):\n    ...\n```\nImplement this in O(log(min(m,n))). Prove your partition invariant, and give the edge case that breaks a solution which only handles equal-length inputs.",
]


def build_corpus(size: int = 300, seed: int = 20260902) -> list[str]:
    """Return up to `size` prompts, interleaved so any prefix stays balanced.

    Interleaving matters because `--limit N` labels a prefix; taking the bands
    in order would give a subset that is entirely one difficulty.
    """
    del seed  # curated, not sampled -- kept for call-site compatibility
    bands = [SIMPLE, MODERATE, HARD]
    prompts: list[str] = []
    for i in range(max(len(b) for b in bands)):
        for band in bands:
            if i < len(band):
                prompts.append(band[i])
    return prompts[:size]
