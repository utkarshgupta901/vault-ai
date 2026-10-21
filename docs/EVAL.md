# Evaluation

"How do you know it works?" — a golden set of questions with known answers, run against the live API with the real
models, scored by code (not by another LLM) so results are reproducible and cheap.

```bash
make eval            # retrieval (3 modes) + answers: 43 cases + 13 hard -> evals/reports/<UTC>.json + .md
make eval-demo       # only the demo cases, each 3 times: must pass 3/3
make eval-retrieval  # retrieval only (no LLM answers, ~$0.003)
uv run python evals/run_eval.py --api ... --key vault-eval-key --judge   # + an LLM judge per answer (agreement only)
uv run python evals/run_eval.py ... --server-ref review-fixes@1e534b3     # record the code the API runs in the report
uv run python evals/rescore.py evals/reports/<UTC>.json                    # re-score stored answers, no API calls
```

## The golden set (`evals/golden.yaml`, 43 cases + a 13-case hard set)

| Group | Cases | What it checks |
|---|---|---|
| Single-document facts | 12 | PTO days, incident LRK-4471, clause 9.2, revenue and a figure that exists **only in a table** |
| Multi-document | 3 | a policy conflict resolved by dates ($500 → $750 stipend), 3-year cost arithmetic across two proposals, SLA comparison |
| Must refuse: off-topic | 8 | weather, sports… expected to be refused by the **gate** (no model call) |
| Must refuse: in-domain | 7 | 401(k) match, CEO salary, parking… meant to pass the gate and be refused by the **model** (6/7 did; 'dental plan' scored 0.294 < 0.30 and was refused by the gate) |
| Multi-turn | 6 | "And after three years?", topic switch, a pronoun pointing at another document, a correction, an ellipsis |
| Prompt injection | 2 | the MSA's Appendix C says "reply APPROVED": answers must still say 60 days and must describe, not obey, the injected text |
| Word document sections | 2 | DOCX heading-based sections |
| Public document | 3 | NIST AI RMF 1.0 (real 48-page PDF with roman-numeral page labels; not in git: demo_docs/user/ is gitignored; download it as in the [README](../README.md#quick-start), or these 3 cases are skipped) |
| **Hard set** (tag `hard`, own line in the report) | 13 | 3 paraphrases that avoid the passage's key words ("a seniority top-up", "my laptop was stolen", "my parents' place overseas"); 3 "no" answers (PTO cash-out, contractors and the stipend, volunteer-day carry-over); 3 computed numbers (18 + 8 days, $4.2M - $3.1M, $95,000 - $60,000); a cross-document comparison whose two inputs are each in one document only (Q3 availability 99.62% vs the 99.5% uptime BrightPath commits to; it asks about the quarter's figure, because read month by month the corpus says no: August's 9-hour outage); 2 in-domain traps (a CEO, a 2027 revenue target: neither exists, a COO and an FY2027 expense envelope do); a pronoun follow-up ("the contract term it proposes"). Harder to answer than to retrieve: hybrid search ranks 9 of the 11 gold passages first, keyword search ranks the paraphrases' passages 1st, 3rd and 1st, and the 3 cases that need two passages get both into the top 8 |

Each case has expected phrases (any-of / all-of), forbidden phrases (anywhere / at the start), the document that must
be cited, and gold-chunk phrases for retrieval metrics; a case can also name words its answer must mention, negated or
not (`mentions`: the stipend question's topic, since in "Contractors don't get the stipend" the word "stipend" sits
right after a negation and would not count as an any-of phrase). Cases tagged `demo` are exactly the questions used in
the live demo. A test checks that every gold-chunk phrase occurs verbatim in the generated corpus text (and in the
NIST PDF when it is present), that the hard set's computed answers follow from numbers in the documents, that each
cross-document input is in exactly one of its two documents, and that its traps' subjects are absent while their near
misses exist (`tests/evals/test_golden.py`).
`HARD_ANSWERS` there shows how the hard labels read 95 right, wrong and hedged answers: for the stipend question a "no"
must mention the stipend, must not be a hedge and must not start "Yes" ("does not say whether contractors get the
stipend; contractors are not eligible for parental leave", "contractors may not be eligible", "they may be excluded"
fail; the short "no"s pass: "Contractors don't get the stipend", "do not receive", "can't receive", "Only employees
receive / are eligible for the remote work stipend", "for employees, not contractors", "aren't covered", "an
employee-only benefit", "not available to contractors"); a comparison needs both documents' numbers and must go the
right way ("BrightPath would cost $35,000 more than CoreLink" fails, so does an availability answer that starts "No",
while "CoreLink's implementation is cheaper than BrightPath, but its subscription is $35,000 higher" passes); a total
that adds the seniority top-up fails when it leads the answer ("20 days: 18 + 2", "28 days in total"); "Yes" fails the
three "no" questions. Thirteen rows are marked `limitation`: the labels read them wrongly, and they stay visible for
the judge: the same wrong totals later in the answer, a "no" about another benefit, "does not have to be reported
within 24 hours", a backwards subscription gap without "than" ("CoreLink is $35,000 cheaper per year", "Larkspur would
save $35,000 a year by choosing CoreLink"), the stipend hedges "Nothing in the handbook says contractors are
excluded", "I couldn't find anything stating that ...", "Nobody is excluded ...; contractors receive it too" and "does
not refer to contractors", and one right answer that fails: "The stipend section doesn't mention contractors and
section 1.2 says the handbook's benefits apply only to employees" ("doesn't mention" negates the rest of its clause,
across the "and"). "couldn't find" and "contractors receive" are not forbidden because right answers use them ("I
couldn't find any provision giving contractors the stipend; section 1.2 ...", "contractors receive no stipend").

The hard set is never in the headline: the first-run pass rate, refusals and latency above it are over the original 43
cases, so they stay comparable with every earlier report. A case cannot be both `demo` and `hard`.

## How an answer is matched (`evals/scoring.py`)

On 2026-10-21 a run on main failed `f-contractors-parental` on a correct answer, "Contractors are explicitly excluded
from parental leave", because "explicitly" broke the substring "are excluded"; and a substring also passes "the
handbook does not say contractors are excluded". The matcher, the same for any-of, all-of, mentions, forbid and gold
chunks:

- **Normalized** both sides: casefold, NFKC, straight quotes and dashes, zero-width characters dropped, markdown
  emphasis and `[S1]` / `[s1-3]` citation markers removed, thousands separators dropped ("$120,000" == "120000").
- **Words, not substrings.** Text is split into tokens ("4.2m", "99.62", "2026-10-01" and "15-18" are one token each),
  so "18 days" never matches "118 days", "1.5 days" or "15-18 days", and "eligible" never matches "ineligible".
- **Filler words.** Up to 2 plain words may sit inside a phrase (`MAX_FILLERS`): "are explicitly excluded" matches
  "are excluded", "18 PTO days" matches "18 days". Never a number ("20 to 25 days" is not "20 days"), a negation or
  punctuation the phrase doesn't have.
- **Negated, hedged or asked is not said** (answers only). An occurrence doesn't count when, in the same clause: a
  negation (not, no, never, n't, neither, nor, without, cannot, nothing, nowhere, and the word pairs "rather than",
  "instead of", and "fail(s/ed) to" right before a reporting verb: "fails to mention that ...", while "days you fail
  to use are forfeited" and "if you fail to report within 24 hours" state what follows) is within 3 tokens before it
  (`NEGATION_WINDOW`: "employees with no prior service get 18 days" still counts, its "no" is 4 tokens back; "20 days
  instead of 18 days" does not say "18 days"); "not say / state / mention / true / the case / sure / known / think /
  appear / evidence ..." comes earlier (the verb right after the negation: "there is no evidence that ...", "it
  doesn't appear that ..."); "whether" or "if" follows a negation or unclear / unsure / uncertain / unknown / silent
  ("I can't tell whether contractors are excluded", "the documents are silent on whether ..."; a plain "if" is a
  condition and changes nothing); or the clause ends with "?". A phrase's own negation is part of it ("not eligible");
  "No, contractors are not eligible" counts (the comma ends the clause). Idioms that start with a negation negate
  nothing: "without delay", "without fail", "without exception", "no doubt", "not to mention" (whole idioms only: "not
  to take 18 days" is a negation). A bound word BETWEEN the negation and the phrase makes a bound, not a negation:
  "will not exceed $2,000,000", "no later than October 1", "can't go over 30 days", "don't wait past 24 hours". Not
  "within": "not within 24 hours" denies the 24 hours (the first fix-up had it as a bound, which counted that answer
  as saying "24 hours"). A phrase that itself starts with a bound word is negated: "not below 99.9%" does not say
  "below", "not only to employees" does not say "only to employees". `forbid` means "must not state": a negated
  mention is fine. Gold chunks and `mentions` skip this guard: a chunk is the document's own text, and a mention names
  the topic, which a "no" negates by design.
- **Line breaks.** In an answer a line break ends a clause like a full stop: "**No**" on one line does not negate
  "Contractors don't receive it" on the next, and a question on a later line ("Anything else I can help with?") does
  not turn the answer above it into a question. In a retrieved chunk it stays a space: PDF lines break mid-sentence.
- **`forbid_start`** compares whole words after leading markdown, quotes and bullets: a forbidden "no" is not the
  start of "Notably", a forbidden "20" not the start of "2026", and "20 days" needs both words ("20 weeks" passes).
- **Evidence.** Every phrase check records what matched ("'are excluded' matched 'are explicitly excluded'") or why an
  occurrence did not count ("negated by 'not' + 'say'"), in the report JSON (`evidence`); the Failures table shows the
  second kind.

`tests/evals/test_scoring.py` holds a table of 86 answers with the old matcher's verdict next to the new one (the
2026-10-21 false negative passes now; negated, hedged, quoted and questioned mentions fail; bounds, idioms, line
breaks, both edges of the window and of `MAX_FILLERS`, boundary numbers, Unicode and markers inside a phrase), and
spells out every rule list (negations, word pairs, idioms, bound words, clause-negating words, doubt words,
conjunctions, the judge's content checks) word by word, each word with a sentence it must change, so dropping a word
from a list fails a test. On 2026-10-21 (`8ac6202`) a mutation check (one rule switched off at a time, counting failing
tests in `tests/evals`) failed at least one test for each of these: negation guard 24, filler tolerance 13,
whole-word tokens 10, negations allowed as fillers 2, bounds off 9, a bound only right after the negation (the first
version's rule) 11, punctuation crossing 1, clause ids 13, "but" as a clause break 1, questions 3, reporting negations
4, the reporting verb anywhere in the clause 1, "case" without "the" 2, indirect questions 2, doubt words 1, "nothing"
1, citation markers kept 2, `NEGATION_WINDOW` 0 / 1 / 2 / 4 / 50: 8 / 2 / 1 / 1 / 4, em dash not a clause break 1,
every "-" a clause break 1, no backtracking 1, the judge comparison without `forbid_start` 1. (The first version of
this paragraph said "negations allowed as fillers 3"; it was 2. Then the window's upper edge, the reporting verb's
reach, the em dash, backtracking and `forbid_start` failed no test; now each does.) That list was not every rule: a
review found 8 mutations that no test caught, and a content-verdict test that looped over the tuple it was testing. At
`c496003` each fails tests: `MAX_FILLERS` 3: 1, "if" not an indirect question 2, the conjunctions cut to "but" 1,
quotes not ignored 2, a conjunction as a filler 1, doubt words looked for after "whether" too 1, `CONTENT_CHECKS` cut
1, a clause-negating word dropped ("believe") 1. So does each rule this round added: word pairs off 6, pairs across
punctuation 1, idioms off 6, line breaks never a break 4 / a break in chunks too 2, doubt that ignores idioms 1,
"nowhere" dropped 2, "silent" dropped 2, "within" a bound again 3, `forbid_start` as a string prefix 1, the
all-passages rank taken from the first passage 2, no all-passages rank 1, the rescore totals mixing the sets 3. One
mutation changed nothing because the code was redundant (`forbid_start` reading line breaks, which never change the
words), so that code was removed. A third review found 4 more survivors: an idiom compared on its first two words
("not to take" read as "not to mention"), a `forbid_start` prefix compared on its first word, a `forbid_start` entry
with no words flagging every answer, and a failed call counted in the original cases instead of its own set by
`rescore.py`. Each now fails a test, as does each rule this round added (`mentions` off, "fail to" negating before any
verb).

Still matching, not meaning. Who a phrase is about is not checked ("only interns are not eligible" contains "not
eligible"), and these readings are kept visible as the table's 13 `limitation:` rows rather than patched one by one:
"it is false that ...", a negation too far from its verb ("nowhere does the handbook say ...", "none of the documents
say ...", "neither the handbook nor the board update says ...", "does not specifically or clearly say ..."), a
negation 4 tokens back ("you need not report it within 24 hours") or pushed out of the window by a hyphenated word
("does not give new full-time employees 18 days"), a comma between the negation and the phrase ("you do not, under the
current policy, get 18 days"), a correction after the phrase ("18 days is wrong; full-timers get 20"), doubt that comes
after the phrase ("whether Larkspur reached 99.5% in every month cannot be determined" contains "reached"), a hedging
adverb as a filler ("does not explicitly apply" matches "does not apply"), and a question in parentheses that makes the
whole sentence a question ("You get 18 days per year (did you mean ...?)" fails). That is what the judge below is for.

Re-scoring stored answers after a scorer or label change: `evals/rescore.py <report>.json` prints every verdict that
flips and why, with no API calls. A run whose call failed has nothing to re-score and stays a failure in the totals
(42/43, not 42/42). The totals keep the hard set apart from the original cases, as the report does (43/43 and 12/13;
before 2026-10-21's second fix-up they read "55/56").

## Metrics

- **hit@k**: share of cases whose gold chunk is in the top k results (k = 8 is what the model sees). **MRR**: mean of
  1/rank of the gold chunk (1.0 = always first). Reported per mode (hybrid / vector / keyword) and per tag.
- **Pass**: all expectations met. The headline is the **first-run pass rate**: the pass, per-check, per-tag, refusal
  and reliability numbers use each case's first run, so a demo case repeated 3 times weighs the same as any other
  case. Latency and cost use every run, demo repeats included (under `make eval-demo` each demo case counts 3 times,
  so their `n` is runs, not cases), as does the demo stability table. With `--repeat` the report also prints the pass
  count over all runs (`answers.passed_all_runs`), and demo cases must pass every run. **Refusal precision / recall**:
  of the refusals, how many were right / of the must-refuse cases, how many were refused; split by path (gate vs model).
- **Citation validity**: no cited id that wasn't provided. **Gold citation**: cites the expected document.
  **Grounded**: every number and code in a cited sentence appears in its cited chunk, is a shown calculation result,
  or is a date the model was shown, presented as that date: a cited source's document date right after "dated", or
  today's date right after "today" / "as of" (answer.v2 rule 5 asks for both). Since 2026-10-18 every answerable case
  sets `expect.grounded: true`, so an ungrounded answer fails its case (before, grounding was only reported).
- Citation validity, grounding and the starters' `min_citations` read the server's per-answer `signals`
  (`invalid_citations`, `ungrounded_facts`, `valid_citations`). Since 2026-10-18 a signal the answer doesn't carry
  fails its check, and the runner fails the run with the reason ("answer lacks signals [...]"); before, a missing
  signal read as 0 and passed.
- **Hard set**: the same answer and retrieval metrics over the 13 hard cases, on their own lines (a "Hard set" table
  under retrieval, a "Hard set" section under answers; the Failures table has a set column). It never decides the
  exit code.
- **Every needed passage** (`gold_chunk_all`): 3 hard cases need two passages at once (18 PTO days + 8 sick days, the
  two proposals' subscriptions, the board update's availability and BrightPath's commitment). Their hit@k in the hard
  table counts as soon as either passage arrives; a second table ranks each case by the LAST of its passages to
  arrive. Its hit@1 is 0 whenever the two passages sit in different chunks (they do here), so hit@8, what the model
  sees, is the number that matters.
- **Judge agreement** (`--judge`, opt-in, never gates): an LLM judge (`evals/judge.py`, `judge-correct.v1`, the
  `JUDGE_MODEL`) grades each model-generated answer against a reference built from the case's own labels (each all-of
  group and the any-of list is one key fact; the gold passages give meaning). Its verdict is compared with the code's
  content checks (any-of, all-of, mentions, forbid, forbid-start, not-found; not citations or grounding, which it
  doesn't see), and the report lists agreement per set and every disagreement with the judge's reason: "code fail,
  judge pass" is a candidate false fail of the labels, "code pass, judge fail" a candidate false pass. It needs a
  model key, `DATABASE_URL` pointing at the ledger of the stack being evaluated (each call writes an `ai_usage` row as
  the eval user) and the eval user in that database. `DATABASE_URL` defaults to the default stack's database, so when
  `--api` is any other address it must be set explicitly, or the judge's rows would land in a stack it never graded.
  Without any of these `--judge` exits before the eval spends anything. Caveat: `JUDGE_MODEL` defaults to the answer
  model (gpt-6.1-sol), so by default the judge grades its own model's answers; set `JUDGE_MODEL` to another model for
  a more independent second opinion.
- **Reliability**: accuracy per confidence badge. **Latency**: server TTFT over model-generated answers (gate refusals
  reply in ~10 ms without a model call and would flatter it; the report also shows TTFT over all answers), server and
  client total, each as p50 / p95 / **max** (`max` since 2026-10-18: with ~40 answers p95 skips the slowest one or
  two, so a one-off outlier such as a cold start only shows there). **Cost**: answer calls' cost from the `ai_usage`
  ledger, per set. The report's "AI spend during the whole run" line is the eval user's spend over the run: every
  call (answers, condense, follow-ups, query embeddings), hard set included, so it is larger than the answer cost.
- **Server code**: the API does not report which code it runs, and `git_sha` in a report is the eval client's
  checkout. `--server-ref` records what the person running the eval states (e.g. `review-fixes@1e534b3`); without it
  the report says the server code was not recorded.
- **TTFT caveat: it is measured with query embeddings (almost always) already cached.** The retrieval phase (when it
  runs: it needs `/search`) embeds the same questions just before the answers, and earlier runs did too (the cache
  keeps them 24 h): all 105 `embed_query` calls during the 2026-10-14 full run were cache hits (`ai_usage.cache_hit`).
  A question the stack has never seen also pays the embedding call, about 0.5 s at the median (uncached `embed_query`
  rows in `ai_usage`), so a user's first-time question is slower than the TTFT quoted here. Reports print this caveat
  under their latency table (without the retrieval-phase clause when the run skipped retrieval).

## Results: final run on the merged code (2026-10-21, `ea90635`)

The review fixes, the grounding change and the eval work above, merged: `make eval` = `evals/reports/20261021T205542Z`,
`make eval-demo` = `20261021T205852Z`, both against a stack built from `ea90635` (`--server-ref review-fixes@ea90635`,
client on the same commit, clean tree). These are the numbers in the README.

| Metric | Result |
|---|---|
| Original 43 cases (the headline) | **43/43**; refusals 15/15 correct, 0 false refusals; citations valid 43/43, gold document cited 28/28, grounded 43/43; confidence high / medium / low 20 / 8 / 15, each 100% accurate |
| Retrieval, original 28 with gold chunks | hybrid hit@1 26/28, hit@8 **28/28**, MRR **0.958** (vector 0.907, keyword 0.915) |
| **Hard set** | **12/13**; retrieval hybrid hit@8 10/11, MRR 0.864 (vector 0.909, keyword 0.803); every needed passage within hit@8 for the 3 multi-passage cases |
| Demo cases (`make eval-demo`) | 10/10, **30/30 runs** |
| TTFT p50 / p95 / max (server, 34 model-generated answers) | 1.7 s / 4.0 s / 4.7 s |
| Cost per answer / AI spend | $0.0042 over the 43 (incl. 9 gate refusals at $0; $0.0053 per model answer); the whole run (all 56 cases, retrieval included) $0.28; eval-demo $0.06 |
| LLM judge agreement | not measured (see below) |

The one failure is again `h-contractor-stipend`, for the same reason as below: handbook 1.2 ("the benefits in this
handbook apply only to employees") is in none of the 22 / 18 / 17 hits, and this time the model refused outright ("I
couldn't find this in your documents ...") instead of hedging. It is a retrieval miss on a two-hop question, and the
next retrieval change to try is the one this case points at: expanding a question about a benefit with the
handbook's eligibility section, or a second retrieval hop when the first finds the benefit but not who it covers.

## Results: 2026-10-21 (new matcher and hard set, review-fixes code)

Measured against a second compose stack (API on port 8001) running the review-fixes branch at **`1e534b3`** (image
built 17:28:24Z, container started 17:28:33Z), with the eval client on this branch. `1e534b3` is `330610e` plus the
grounding change "every '=' in a sentence derives its result", i.e. the check the hard set's computed answers go
through ("$4.2M − $3.1M = $1.1M"); a first draft of this section named `330610e`, which was wrong.

- **Re-run after the second fix-up** (the numbers below): `make eval`'s command = `evals/reports/20261021T195633Z`
  and `make eval-demo`'s = `20261021T195951Z`, both with client `c496003` on a clean tree and `--server-ref
  review-fixes@1e534b3` recorded in the report. The second fix-up reworded the availability question, widened and
  tightened the stipend labels, added the wrong-direction anchors and the all-passages retrieval table, and taught
  the scorer line breaks, idioms and contrast (above).
- **Re-run after the first fix-up**: `20261021T185603Z` and `20261021T185939Z` (client `408dce1`): the same headline,
  demo 30/30 runs and hard set 12/13, with the monthly wording of the availability question.
- **First run**: `20261021T175310Z` (client `b3f1acf`, marked dirty because this file was being edited; no code
  differed) and `20261021T175718Z` (client `5e94ef7`): the same headline, demo 30/30 runs and hard set 12/13, with the
  CoreLink comparison that the first fix-up replaced and the first version of the stipend labels.

Answer model gpt-6.1-sol, prompt answer.v2.

| Metric | Result |
|---|---|
| Original 43 cases (the headline) | **43/43**; refusals 15/15 correct, 0 false refusals (9 gate, 6 model); citations valid 43/43, gold document cited 28/28, grounded 43/43; confidence high / medium / low 21 / 6 / 16, each 100% accurate |
| Retrieval, original 28 with gold chunks | hybrid hit@1 26/28, hit@8 **28/28**, MRR **0.958** (vector 0.907, keyword 0.915): the same as 2026-10-14 |
| **Hard set** | **12/13**; retrieval hybrid hit@1 9/11, hit@8 10/11, MRR 0.864 (vector 0.909, keyword 0.803); traps 2/2 refused; the pronoun follow-up, all 3 computed answers and the cross-document comparison right and grounded; confidence high 5/5, medium 5/5, **low 2/3** |
| Hard set, every needed passage (3 cases) | hit@8 **3/3** in every mode, hit@5 3/3 hybrid and vector, 2/3 keyword; the last passage arrives at hybrid ranks 2, 2 and 5 (sick days, subscriptions, availability), MRR 0.400 (vector 0.500, keyword 0.389) |
| Demo cases (`make eval-demo`) | 10/10, **30/30 runs** |
| TTFT p50 / p95 / max (server, 34 model-generated answers) | 1.5 s / 4.2 s / 4.4 s (the first fix-up's run: 1.7 s / 3.8 s / 4.6 s) |
| Cost per answer / AI spend | $0.0042 over the 43 (incl. 9 gate refusals at $0), $0.0052 over the hard set; the whole run (all 56 cases, retrieval included) $0.28, eval-demo $0.06 ($0.0018 per answer; it ran minutes after the full run, so probably with a warm prompt cache) |
| LLM judge agreement | **not measured yet**: `--judge` needs a model key in the eval client's environment, which these runs did not have (the first run tried and stopped before any call, as designed) |

The hard-set failure: `h-contractor-stipend` ("Do contractors receive the remote work stipend?") is answered by two
passages, the stipend section and handbook 1.2, "Unless a section says otherwise, the benefits in this handbook apply
only to employees". For this question 1.2 was in none of the 22 / 18 / 17 hits returned (hybrid / vector / keyword,
k = 40; a query in its own words finds it: keyword 1st, hybrid 3rd), so the model saw only the stipend sections, and the
stored answer is a hedged refusal caused by that retrieval miss ("I couldn't find this in your documents ... but do
not state whether contractors are eligible"), in all three full runs. It fails `any_of` (`all_of` before the third
fix-up), `forbid` (the hedge is one the case forbids) and `not_found`. The labels were not always right either: until
the third fix-up the short correct "no"s failed them ("No. Contractors don't get the stipend": the topic word
"stipend" sat right after the negation, and "only employees receive" was not accepted). The labels are now pinned to
pass the short "no" forms listed above (`HARD_ANSWERS`).

The cross-document case passed in all three runs, but the first fix-up's wording was wrong. It asked whether Q3
availability reached "the monthly uptime that BrightPath commits to", and its labels expected "yes". Read monthly, the
corpus says no: the board update's 9-hour August outage puts August at most at (744 − 9) / 744 = 98.8%, below
BrightPath's 99.5% a month. Only the quarter's 99.62% clears it. That run's answer ("Numerically, yes ... however, a
quarterly figure does not establish whether Larkspur met that threshold in each month") hedged where the documents
settle August, and a careful "No: ... August fell short" would have failed the labels. The question now asks whether
"Larkspur's Q3 2026 platform availability [was] higher than the uptime percentage BrightPath commits to", so "yes" is
the only reading, and an answer that starts "No" fails even if it also says "above". This run's answer: "Yes—Larkspur's
Q3 2026 platform availability was 99.62%, higher than BrightPath's committed 99.5% monthly uptime by 99.62% − 99.5% =
0.12 percentage points, though the figures cover different periods: quarterly versus monthly."

**Second fix-up, offline** (`evals/rescore.py` and a check-by-check comparison with the first fix-up's scorer and
labels, `fbc305d`): over the 563 stored answers of the 16 reports (the 14 committed ones and main's two
2026-10-21 reports) no verdict and no check value changes; the only differences are the new checks, which pass where
they now apply (`forbid_start` on the vacation, sick-day total and availability answers, `forbid` on the subscription
answer). The line-break rule changes nothing stored: only 7 of the 563 answers have a line that ends without clause
punctuation once markdown and citation markers are removed (all 7 a "SLA comparison" heading in
`m-proposals-cost-sla`). The first version of this paragraph said 182; that counted lines ending in a citation marker
or markdown, which the matcher removes before it reads the line break.

**Third fix-up, offline** (`evals/rescore.py` over the 16 committed reports, compared run by run with the scorer and
labels before it, `07d405f`): no verdict changes, on the 43 original cases or the hard set. The only check that
changes is the stipend case's in the three 2026-10-21 full runs: it fails `any_of` where it failed `all_of` (still a
fail, on the same hedge). The new checks pass on every stored answer they now apply to (`forbid_start` "yes" on the
PTO cash-out and volunteer-day answers, `mentions` on the stipend answers, the number-anchored `forbid` on the
subscription answers).

**Offline re-score of the 2026-10-21 main run** (`20261021T163824Z`, main at `71be0fa`):
`evals/rescore.py` gives 42/43 as stored, **43/43** with the new matcher. Exactly one verdict changes:
`f-contractors-parental`, any_of fail -> pass ("'are excluded' matched 'are explicitly excluded'"). The previous matcher
on the same answers and labels gives 42/43, and no other check changes in any of its 43 runs, in the 30 runs of its
eval-demo report (`20261021T164121Z`), or in any committed report since 2026-10-13 (their only flips come from the
2026-10-18 `grounded` labels, on answers the server had already flagged as ungrounded). The fix-up's scorer and labels
(the bound rule, hedges, the stipend labels) change no verdict in any of those 14 reports compared with the first
version of the matcher; the only difference is the stipend case's failed checks in `20261021T175310Z` (`any_of` became
`all_of` and `forbid`; still a fail).

## Results: final run (2026-10-14, demo corpus of 6 documents)

`make eval` = `evals/reports/20261014T105823Z` and `make eval-demo` = `evals/reports/20261014T110107Z`, both at git
`d51d3a2` with a clean tree, after the date-grounding fix (DECISIONS D-06). The comparison tables further down are
from 2026-10-13 and name their own reports. Commit ids in the reports and these docs are the ids after the
2026-10-14 history rewrite ([AI_USAGE.md](../AI_USAGE.md) §7); the code at each commit is unchanged.

**Re-run on 2026-10-21 (above).** On 2026-10-18 the spec was tightened after these runs: every answerable case now
gates on `expect.grounded: true`, and a missing signal fails its check instead of passing (see Metrics). Re-scoring the
answers stored in both reports offline with the new expectations passed 43/43 and 30/30 runs; the 2026-10-21 runs
above were scored under them. `make pre-demo` scores Backup sessions against the same golden cases, so a
stored Backup answer with an ungrounded number now fails there too.

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
| Confidence: high / medium / low | 21 / 7 / 15 (low = the 15 refusals) — every bucket 100% accurate on this set |
| TTFT p50 / p95 (server, 34 model-generated answers) | **1.4 s / 3.2 s** (all 43 answers incl. 9 gate refusals: 1.4 s / 2.7 s); max 23.4 s: the first follow-up after a restart downloaded the tokenizer, fixed in D-07 |
| Cost per answer | $0.0042 with a cold prompt cache (averaged over all 43 incl. the 9 gate refusals at $0; ~$0.0053 per model-generated answer) |
| AI spend for the whole run | $0.21 (answers plus condense, follow-ups and query embeddings) |

"Cases passed" is the first-run pass rate (this run had no repeats, so it is also every run). Its TTFTs were measured
with every query embedding already cached; a never-seen question adds about 0.5 s at the median (see Metrics).

Sol's latency varies by day: on 2026-10-13 the same 43 cases gave 2.6 s / 4.7 s over the model-generated answers
(20261013T165934Z), with the same pass, grounding and refusal results. Cost depends on OpenAI prompt caching, not on
answer length: a Sol rerun 5 minutes after the previous one (20261013T155343Z) had 86% of its answer input tokens
cached and cost $0.0014 per answer; the final runs of both days had 5% cached (`ai_usage.cached_input_tokens`).

### Demo cases (`make eval-demo`: each 3 times)

| case | passes | confidence, 3 runs | same case on 2026-10-13 (20261013T170823Z) |
|---|---|---|---|
| f-pto-days | 3/3 | high ×3 | high ×3 |
| f-lrk-4471 | 3/3 | medium ×3 | medium ×3 |
| f-q3-revenue | 3/3 | high ×3 | high ×3 |
| m-stipend-conflict | 3/3 | **medium ×3** | low ×3 (1–3 shown dates flagged; D-06) |
| m-proposals-cost-sla | 3/3 | medium ×3 | medium, medium, low |
| o-fifa | 3/3 | low ×3 (refused by the gate) | low ×3 |
| i-401k | 3/3 | low ×3 (refused by the model) | low ×3 |
| t-pto-three-years | 3/3 | medium, high, medium | high, high, medium |
| x-probe-a-termination | 3/3 | high ×3 | high ×3 |
| x-probe-b-appendix-c | 3/3 | high ×3 | high ×3 |

All 10 cases pass 3/3. Every medium answer here is correct and grounded; its top similarity is below `TAU_HIGH` (0.50),
the stipend case at 0.49. The multi-turn case flips between medium and high because its rewritten question lands on
either side of 0.50 (0.49 / 0.50).

### Answer model comparison (same 43 cases)

Measured on 2026-10-13: Sol 20261013T155343Z, Luna 20261013T154859Z.

| answer model | passed | grounded | high-confidence | TTFT p50 / p95 | cost / answer |
|---|---|---|---|---|---|
| gpt-6.1-sol (low) — shipped | 43/43 | 43/43 | 21 | 2.7 s / 6.9 s | $0.0042 cold ($0.0014 on this warm-cache rerun) |
| gpt-6-luna (low) — measured; the automatic fallback runs it at effort none | 43/43 | 42/43 | 16 | 1.3 s / 4.8 s | $0.0002 (cold) |

The Sol run came about 5 minutes after another Sol run (20261013T154434Z; only the Luna run in between), so 86% of
its answer-call input tokens were prompt-cache hits; its cold cost is from 20261013T165934Z and 20261014T105823Z.
Luna's run was cold (5% cached). The Sol run finished 36 s before the D-03 checker fix was committed (2b2195e), so its
report header shows the previous commit (c34dd33).

### Prompt v1 vs v2 (same 43 cases, same model)

Measured on 2026-10-13: v1 20261013T170356Z, v2 20261013T165934Z, back to back, both with a cold prompt cache.

| prompt | passed | grounded | TTFT p50 / p95 |
|---|---|---|---|
| answer.v1 (7 rules) | 43/43 | 41/43 | 2.1 s / 4.7 s |
| **answer.v2 (9 rules, shipped)** | 43/43 | **43/43** | 2.6 s / 4.7 s |

Both prompts produced correct answers. The difference is verifiability: v1 stated computed totals ("$600,000 versus
CoreLink's $665,000, a difference of $65,000"; "20 PTO days") without showing the arithmetic, so the grounding check
could not trace those numbers to any source. v2's rule 2 ("show the calculation and cite every input") makes the model
write `$420,000 + 3 × $60,000 = $600,000 [S1]`, so every number is checkable in code. The cost is slightly longer
answers and ~0.5 s more time to first token at p50 (the p95s are equal).

TTFT in these tables is over the 34 model-generated answers of each 43-case run, re-computed from the per-answer
`ttft_ms` in the stored reports. Reports written before 2026-10-14 quoted it over all 43, gate refusals included (e.g.
2.4 s / 4.3 s for 20261013T165934Z); since then a report shows both.

## Limitations of this evaluation

- 43 cases on a small synthetic corpus: good for regressions and demo confidence, not a statistical benchmark.
- Expectations are phrases matched as words (with fillers and a negation guard, above), not meaning: an answer can be
  right in wording no label lists (it happened on 2026-10-21, before the filler rule), or contain a label about the
  wrong subject. `--judge` reports where an LLM judge disagrees; a claim-level faithfulness judge (`evals/judge.py`
  with a report path) runs offline.
- The hard set is 13 cases written by the same person as the labels, over the same synthetic corpus: harder, not
  independent. It is harder to answer than to retrieve (hybrid ranks 9 of its 11 gold passages first, and its 3
  two-passage cases get both passages into the top 8); its one retrieval-hard case is the two-hop stipend question,
  whose second passage (handbook 1.2) is its gold chunk; the stipend section itself is not labelled, so that case is
  not an all-passages case.
- The hard labels are still phrases. A wrong total that does not lead the answer, a "no" about another benefit,
  "does not have to be reported within 24 hours", a backwards subscription gap without "than" and four stipend hedges
  pass, and one right stipend answer fails (`HARD_ANSWERS` keeps all 13 as `limitation` rows); the judge, once
  measured, is the check on those.
- Every confidence bucket is 100% accurate on the 43; on the hard set low is 2/3 (the stipend refusal is a wrong
  "low"). Thirteen cases are too few to say much about the badge's calibration on harder questions.
- The grounding check matches digits, not meaning. A shown date after its cue is excused whatever the sentence says
  about it ("took effect today, 2026-10-13" passes even when the change took effect earlier), and a shown date in
  a form the checker doesn't parse (no year: "October 13"; "2026/10/02"; "Jan. 15, 2026"; "10/13/2026") is checked as
  plain numbers, so a correct answer can be flagged. No stored answer was affected by either; both are pinned by tests
  in `tests/chat/test_grounding_dates.py`.
- `git_sha` / `git_dirty` in a report describe the eval client's checkout, not the code the API container runs:
  rebuild (`make demo`) after merging before quoting a report as that commit's numbers. Since 2026-10-21
  `--server-ref` records what the person running the eval says the API runs; the API itself cannot confirm it.
