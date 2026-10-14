# Evaluation

"How do you know it works?" — a golden set of questions with known answers, run against the live API with the real
models, scored by code (not by another LLM) so results are reproducible and cheap.

```bash
make eval            # retrieval (3 modes) + answers for all 43 cases  -> evals/reports/<UTC>.json + .md
make eval-demo       # only the demo cases, each 3 times: must pass 3/3
make eval-retrieval  # retrieval only (no LLM answers, ~$0.003)
```

## The golden set (`evals/golden.yaml`, 43 cases)

| Group | Cases | What it checks |
|---|---|---|
| Single-document facts | 12 | PTO days, incident LRK-4471, clause 9.2, revenue and a figure that exists **only in a table** |
| Multi-document | 3 | a policy conflict resolved by dates ($500 → $750 stipend), 3-year cost arithmetic across two proposals, SLA comparison |
| Must refuse: off-topic | 8 | weather, sports… expected to be refused by the **gate** (no model call) |
| Must refuse: in-domain | 7 | 401(k) match, dental plan, CEO salary… pass the gate, refused by the **model** |
| Multi-turn | 6 | "And after three years?", topic switch, a pronoun pointing at another document, a correction, an ellipsis |
| Prompt injection | 2 | the MSA's Appendix C says "reply APPROVED": answers must still say 60 days and must describe, not obey, the injected text |
| Word document sections | 2 | DOCX heading-based sections |
| Public document | 3 | NIST AI RMF 1.0 (real 48-page PDF with roman-numeral page labels) |

Each case has expected substrings (any-of / all-of, normalized), forbidden substrings, the document that must be
cited, and gold-chunk substrings for retrieval metrics. Cases tagged `demo` are exactly the questions used in the live
demo.

## Metrics

- **hit@k**: share of cases whose gold chunk is in the top k results (k = 8 is what the model sees). **MRR**: mean of
  1/rank of the gold chunk (1.0 = always first). Reported per mode (hybrid / vector / keyword) and per tag.
- **Pass**: all expectations met. **Refusal precision / recall**: of the refusals, how many were right / of the
  must-refuse cases, how many were refused; split by path (gate vs model).
- **Citation validity**: no cited id that wasn't provided. **Gold citation**: cites the expected document.
  **Grounded**: every number and code in a cited sentence appears in its cited chunk, is a shown calculation result,
  or is a date the model was shown, presented as that date: a cited source's document date right after "dated", or
  today's date right after "today" / "as of" (answer.v2 rule 5 asks for both).
- **Reliability**: accuracy per confidence badge. **Latency**: server TTFT over model-generated answers (gate refusals
  reply in ~10 ms without a model call and would flatter it; the report also shows TTFT over all answers), server and
  client total. **Cost**: answer calls' cost from the `ai_usage` ledger.

## Results (2026-10-13, demo corpus of 6 documents)

### Retrieval

| mode | hit@1 | hit@5 | hit@8 | MRR |
|---|---|---|---|---|
| **hybrid (shipped)** | 26/28 (93%) | 28/28 (100%) | **28/28 (100%)** | **0.958** |
| vector only | 25/28 (89%) | 26/28 (93%) | 26/28 (93%) | 0.907 |
| keyword only | 24/28 (86%) | 27/28 (96%) | 27/28 (96%) | 0.915 |

Before the identifier-boost fix (DECISIONS D-02) hybrid was 26/28 at hit@8 with MRR 0.891 — worse than either leg.
Caveat we state openly: the golden questions often reuse the documents' own words, which flatters keyword search;
real users paraphrase more, which is what the vector leg is for. A paraphrased retrieval set is the next improvement.

### Answers (answer model gpt-6.1-sol, prompt answer.v2)

| Metric | Result |
|---|---|
| Cases passed | **43/43** (demo 10/10, multi-doc 3/3, multi-turn 6/6, injection 2/2, table 2/2, public doc 3/3) |
| Refusals | 15/15 correct, **0 false refusals**; 9 by the gate (no model call), 6 by the model |
| Citations valid / gold document cited | 43/43 / 28/28 |
| Grounded answers | 43/43 |
| Confidence: high / medium / low | 20 / 7 / 16 (low = the 15 refusals + 1) — every bucket 100% accurate on this set |
| TTFT p50 / p95 (server, 34 model-generated answers) | 2.6 s / 4.7 s (final run; earlier runs 2.7 s / 6.9 s — Sol's tail varies) |
| Cost per answer | ~$0.0014–0.0042 (varies with answer length and reasoning tokens) |

### Answer model comparison (same 43 cases)

| answer model | passed | grounded | high-confidence | TTFT p50 / p95 | cost / answer |
|---|---|---|---|---|---|
| gpt-6.1-sol (low) — shipped | 43/43 | 43/43 | 21 | 2.7 s / 6.9 s | $0.0014 |
| gpt-6-luna (low) — fallback | 43/43 | 42/43 | 16 | 1.3 s / 4.8 s | $0.0002 |

### Prompt v1 vs v2 (same 43 cases, same model)

| prompt | passed | grounded | TTFT p50 / p95 |
|---|---|---|---|
| answer.v1 (7 rules) | 43/43 | 41/43 | 2.1 s / 4.7 s |
| **answer.v2 (9 rules, shipped)** | 43/43 | **43/43** | 2.6 s / 4.7 s |

Both prompts produced correct answers. The difference is verifiability: v1 stated computed totals ("$600,000 versus
CoreLink's $665,000, a difference of $65,000"; "20 PTO days") without showing the arithmetic, so the grounding check
could not trace those numbers to any source. v2's rule 2 ("show the calculation and cite every input") makes the model
write `$420,000 + 3 × $60,000 = $600,000 [S1]`, so every number is checkable in code. The cost is slightly longer
answers and ~0.5 s more time to first token at p50 (the p95s are equal).

TTFT in these tables is over the 34 model-generated answers of each 43-case run (re-computed from the stored reports;
reports written before 2026-10-14 quoted it over all 43, gate refusals included, e.g. 2.4 s / 4.3 s for the final run).

## Limitations of this evaluation

- 43 cases on a small synthetic corpus: good for regressions and demo confidence, not a statistical benchmark.
- Expectations are substring-based; an answer can be right in wording we didn't anticipate (none were in these
  runs). A claim-level LLM judge (`evals/judge.py`) exists for faithfulness spot checks and is run offline.
- Every confidence bucket is 100% accurate here, so the badge's calibration is untested on harder questions.
- The grounding check matches digits, not meaning. A shown date after its cue is excused whatever the sentence says
  about it ("took effect today, 2026-10-13" passes even when the change took effect earlier), and a shown date in
  a form the checker doesn't parse (no year: "October 13"; "2026/10/02"; "Jan. 15, 2026"; "10/13/2026") is checked as
  plain numbers, so a correct answer can be flagged. No stored answer was affected by either; both are pinned by tests
  in `tests/chat/test_grounding_dates.py`.
- `git_sha` / `git_dirty` in a report describe the eval client's checkout, not the code the API container runs:
  rebuild (`make demo`) after merging before quoting a report as that commit's numbers.
