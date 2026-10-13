# Design decisions (with the evidence behind them)

Each entry: the decision, the alternatives, and what we measured. Numbers come from `evals/reports/` runs on
2026-10-13 (43-case golden set: 28 answerable incl. multi-doc / multi-turn / tables / injection probes, 15 unanswerable).

## D-01 Answer model: gpt-6.1-sol (effort low), with gpt-6-luna as automatic fallback
| answer model | passed | grounded | high-confidence | TTFT p50 / p95 | cost / answer |
|---|---|---|---|---|---|
| gpt-6.1-sol (low) | 43/43 | **43/43** | **21** | 2.5 s / 6.6 s | $0.0014 |
| gpt-6-luna (low)  | 43/43 | 42/43 | 16 | **1.2 s / 3.1 s** | **$0.0002** |

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
