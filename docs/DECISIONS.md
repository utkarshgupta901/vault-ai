# Design decisions (with the evidence behind them)

Each entry: the decision, the alternatives, and what we measured. Numbers come from `evals/reports/` runs on
2026-10-13 and 2026-10-14 (43-case golden set: 28 answerable incl. multi-doc / multi-turn / tables / injection probes,
15 unanswerable); each table names its report ids.

## D-01 Answer model: gpt-6.1-sol (effort low), with gpt-6-luna as automatic fallback
| answer model | passed | grounded | high-confidence | TTFT p50 / p95 (34 model answers) | cost / answer |
|---|---|---|---|---|---|
| gpt-6.1-sol (low) | 43/43 | **43/43** | **21** | 2.7 s / 6.9 s | $0.0042 cold ($0.0014 on a warm-cache rerun) |
| gpt-6-luna (low)  | 43/43 | 42/43 | 16 | **1.3 s / 4.8 s** | **$0.0002** (cold) |

Sources: Sol 20261013T155343Z, Luna 20261013T154859Z (2026-10-13). The Sol run came about 5 minutes after another Sol
run (20261013T154434Z; only the Luna run in between), so 86% of its answer-call input tokens were prompt-cache hits
(`ai_usage.cached_input_tokens`) and it cost $0.0014 per answer; with a cold cache, as in the Luna run, Sol costs
$0.0042 (20261013T165934Z, 20261014T105823Z), about 20× Luna. The Sol run finished 36 s before the D-03 checker fix
was committed (2b2195e), so its report header shows the previous commit (c34dd33).

Both pass every case, so this is a quality-vs-latency call. Grounded, well-cited answers are the product, so Sol
answers; if Sol's first token takes > `ANSWER_TTFT_TIMEOUT_S` (10 s) or it errors before the first token, the same
request is retried once on Luna (warning `fallback_model`). That retry runs at `FAST_EFFORT` (`none`); the table
measured Luna at `low`, as `make demo-luna` runs it. Switching is env-only: `ANSWER_MODEL=gpt-6-luna`. Cheap/fast
work (condense, follow-ups, insights, summaries) always uses Luna. Sol's latency varies by day: the final run on
2026-10-14 (20261014T105823Z) gave 1.4 s / 3.2 s over the same 34 model answers.

## D-02 Identifier boost in hybrid retrieval (found by the eval)
First retrieval eval: hybrid was *worse* than either leg alone (MRR 0.891 vs vector 0.907 / keyword 0.915).
Cause: a chunk found only by the keyword leg's identifier tier (e.g. "section 9.2") scores 1/61 in RRF and loses to
chunks that are merely okay on both legs (~2/65); the per-document cap then removed it entirely.
Fix: an exact identifier match also counts as a rank-1 semantic hit (+1/(RRF_K+1)), and structural references
("Appendix C", "section 9", "exhibit B") join the identifier tier as phrase queries.
Result: hybrid hit@8 93% -> **100%**, MRR 0.891 -> **0.958** (now better than both legs; 20261013T153712Z →
20261013T153950Z). Regression tests added.

## D-03 Grounding checker: derived values and refusals
The deterministic checker flagged 3 correct answers as ungrounded (confidence "low"): a calculated total stated
before its calculation ("20 days: 18 + 2 = 20"), totals reused in a later calculation ("$665k - $600k = $65k"), and a
refusal naming the thing it did not find ("no 401(k) match"). Now: a value shown as a calculation result anywhere in
the answer is derived (its inputs are checked instead), and refusals are not fact-checked. Inputs that are neither in
the sources nor derived are still caught (unit-tested). Dates the prompt showed the model came later: D-06.

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

## D-06 Dates the model was shown count as grounded (found by a docs-accuracy audit)
Rule 5 of answer.v2 asks the model, when sources disagree, to use the document dates and today's date to say which is
current. Those dates reach the model through the `<source>` tag's date and the "Today's date" line, but the checker
only read chunk text. So the stipend demo answer, which was correct ("the handbook dated 2026-01-15 ... effective as
of today, 2026-10-13"), had 1–3 "ungrounded" facts and showed Low confidence in 3/3 runs of 20261013T170823Z. The
pass/fail line stayed green; a docs-accuracy audit found it by reading the eval-demo JSON.
Fix: a shown date counts as grounded only where it is presented as that date. The cited source's document date is
excused right after "dated"; today's date right after "today" / "today is" / "as of". The prompt and the checker share
one clock read per turn, so a turn that crosses midnight can't make them disagree. A wrong date, a shown date used as
an event date ("took effect on 2026-10-02"), and a wrong figure next to a shown date are still flagged
(`tests/chat/test_grounding_dates.py`).
Result: the stipend answer is Medium in 3/3 runs of 20261014T110107Z. Medium, not High, because its top similarity
is 0.49 < `TAU_HIGH` 0.50; I did not lower the threshold to flatter a demo case.
Trade-off: other date forms (no year, "2026/10/02", "Jan. 15, 2026", "10/13/2026") are checked as plain numbers, so a
correct answer can still be flagged; a known limit, listed in [EVAL.md](EVAL.md#limitations-of-this-evaluation).

## D-07 Tokenizer files are baked into the image (found by reading per-answer TTFT)
History is capped by tokens, so the first follow-up question after an API restart was the first call to tiktoken.
With no cache it downloaded its encoding (3.6 MB) synchronously inside the request, which blocked the event loop: in
20261014T105823Z that answer's TTFT was 23.4 s (condense 1.7 s, the answer call 3.2 s; the rest was the download).
The p95 (3.2 s over 34 answers) hid it; the skeptic verifier of the docs pass found it in the per-answer JSON. In a
live demo it would have been the first "And after three years?" after `make demo`.
Fix: the Dockerfile sets `TIKTOKEN_CACHE_DIR` and fetches both encodings at build (o200k_base for the API,
cl100k_base for the chunker), and the API loads its encoding at startup in a thread (`tokenizer_ready`, ~0.4 s from
the baked files). Regression tests: `tests/platform/test_tokenizer_warmup.py`.
Result: right after a restart the same follow-up had a TTFT of 4.7 s (condense 2.2 s + Sol).
Lesson: a p95 over a small sample says nothing about the one slow request a demo will hit; read the maximum too.
