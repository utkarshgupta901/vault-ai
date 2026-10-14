# Design decisions (with the evidence behind them)

Each entry: the decision, the alternatives, and what we measured. Numbers come from `evals/reports/` runs on
2026-10-13 (43-case golden set: 28 answerable incl. multi-doc / multi-turn / tables / injection probes, 15 unanswerable).

## D-01 Answer model: gpt-6.1-sol (effort low), with gpt-6-luna as automatic fallback
| answer model | passed | grounded | high-confidence | TTFT p50 / p95 (34 model answers) | cost / answer |
|---|---|---|---|---|---|
| gpt-6.1-sol (low) | 43/43 | **43/43** | **21** | 2.7 s / 6.9 s | $0.0014 |
| gpt-6-luna (low)  | 43/43 | 42/43 | 16 | **1.3 s / 4.8 s** | **$0.0002** |

Both pass every case, so this is a quality-vs-latency call. Grounded, well-cited answers are the product, so Sol
answers; if Sol's first token takes > `ANSWER_TTFT_TIMEOUT_S` (10 s) or it errors before the first token, the same
request is retried once on Luna (warning `fallback_model`). Switching is env-only: `ANSWER_MODEL=gpt-6-luna`.
Cheap/fast work (condense, follow-ups, insights, summaries) always uses Luna.

## D-02 Identifier boost in hybrid retrieval (found by the eval)
First retrieval eval: hybrid was *worse* than either leg alone (MRR 0.891 vs vector 0.907 / keyword 0.915).
Cause: a chunk found only by the keyword leg's identifier tier (e.g. "section 9.2") scores 1/61 in RRF and loses to
chunks that are merely okay on both legs (~2/65); the per-document cap then removed it entirely.
Fix: an exact identifier match also counts as a rank-1 semantic hit (+1/(RRF_K+1)), and structural references
("Appendix C", "section 9", "exhibit B") join the identifier tier as phrase queries.
Result: hybrid hit@8 93% -> **100%**, MRR 0.891 -> **0.958** (now better than both legs). Regression tests added.

## D-03 Grounding checker: derived values and refusals
The deterministic checker flagged 3 correct answers as ungrounded (confidence "low"): a calculated total stated
before its calculation ("20 days: 18 + 2 = 20"), totals reused in a later calculation ("$665k - $600k = $65k"), and a
refusal naming the thing it did not find ("no 401(k) match"). Now: a value shown as a calculation result anywhere in
the answer is derived (its inputs are checked instead), and refusals are not fact-checked. Inputs that are neither in
the sources nor derived are still caught (unit-tested).
Later (demo eval, m-stipend-conflict "low" in 3/3 runs): correct answers quoted the dates the prompt shows ("the handbook
dated 2026-01-15", "as of today, 2026-10-13"), which no chunk contains. Such a date now counts as grounded, but only
right after its cue ("dated" for a cited source's document date, "today" / "as of" for today's): the same date stated
as when something happened ("took effect on 2026-10-02") is still checked, since that mix-up is what the case probes.

## D-04 Readiness checks must be bounded and single-flight (incident found by adversarial review)
`/health/ready` used to run a Celery `inspect` broadcast on every request. Celery's broadcast holds a connection from
the shared broker pool (10) while kombu borrows a second one for its producer; about 10 overlapping readiness calls
deadlocked the pool. Every stuck call also pinned one of anyio's 40 worker threads; once they were gone, uploads hung
and so did new OpenAI connections (the TLS handshake runs `wrap_bio` in a worker thread), so chat timed out.
Fix: the worker check runs on its own broker connection, single-flight with a 10 s cache; every readiness check has a
2 s timeout and they run concurrently; publishing has a 5 s bound and broker sockets have timeouts, so a stuck broker
becomes `503 QUEUE_UNAVAILABLE` instead of a hang; after 3 consecutive provider failures the API rebuilds its AI client.
Regression test: 20 concurrent readiness calls, then a real publish (deadlocks on the old code, passes in ~1 s now).

## D-05 Citation support is measured against the whole cited chunk
Manual testing on a long real-world document (~49k words, not part of the repo) showed well-cited answers marked "low confidence": a citation was "weak"
when the words of all answer sentences citing it overlapped < 20% with the single best sentence of the chunk. Several
bullets citing one chunk pool more words than any single sentence holds (e.g. 16% vs one sentence, 79% vs the whole
chunk). Support is now measured against the whole chunk; the popover still shows the single best sentence.
