# How AI coding tools were used to build Vault AI

**Tool:** Claude Code (Opus 5.5) in the terminal: plan mode, multi-agent workflows, subagents working in isolated
git worktrees, and the evaluation harness as the arbiter. **Time:** one day, plus a pre-demo pass the next day.
**Result:** ~9.3k lines of Python in app/ (plus ~8.3k in tests), ~5.5k lines of TypeScript, ~615 tests, a 43-case
evaluation, deployment config.

The short version: I used AI to **explore, build in parallel, and attack its own work**, and kept the decisions,
contracts and verification with me. The chronological log with timestamps is in
[docs/ai-usage/log.md](docs/ai-usage/log.md); the design-review artifacts are in
[docs/ai-usage/design-review/](docs/ai-usage/design-review/).

## 1. Planning: a design panel instead of a single plan

Before any code, a 9-agent workflow produced the design:

| Step | Agents | Output |
|---|---|---|
| Research | 2 | current library versions and API shapes (FastAPI native SSE, OpenAI SDK v3 Responses API, pgvector 0.8 iterative scans, Celery 5.6 limits), model ids and prices, RAG best practice, all checked on the web with sources |
| Proposals | 3 | three complete designs through different lenses: *ship a perfect demo*, *AI-first innovation*, *production and scale* |
| Judge | 1 | scorecard against the case study's three criteria + feasibility + explainability; merged the best parts |
| Cross-examination | 2 | adversarial "panel interviewers" (RAG depth; systems and product) asked 50 hard questions of the merged design and found 31 gaps, e.g. an AND-only keyword query that would have silently disabled hybrid search |
| Finalize | 1 | the final design with every cheap fix applied |

**What I decided:** OpenAI as provider, a React dashboard (the panel recommended static HTML), S3-compatible
storage so the app is cloud-deployable (the panel used a shared local volume), Render as the target, a public
document (NIST AI RMF) as the real-world sample, so everything in the repo and demo is shareable, and how I wanted to be
taught along the way.

## 2. Verify assumptions before writing code

AI knowledge goes stale, so every external fact the design depended on was probed live first:
- **OpenAI SDK v3 + GPT-6 models**: a probe script confirmed `responses.parse`, the streaming event names, usage
  fields, embedding dimensions, and that `gpt-6.1-sol` rejects reasoning effort `none` (Luna accepts it).
- **MinIO's free images had been withdrawn**: three S3-compatible alternatives were pulled and tested with the app's
  exact boto3 settings; RustFS was chosen.
- **Render specifics** (paid-only workers, Key Value `noeviction`, R2 checksum gotcha) came from a research agent
  and shaped `render.yaml`.

## 3. Building: contracts first, then parallel agents

1. **I (with Claude) wrote the foundation myself**: config, the hand-written SQL schema, ORM models, the error
   registry, auth, the `LLMProvider` seam with a deterministic offline Fake, storage, Celery wiring, the upload path,
   frozen response schemas, and `CLAUDE.md` with 14 hard rules (e.g. *never hold a DB session across an LLM await*,
   *`openai` is imported in one file only*, *every model call writes a cost row*).
2. **Six agents built in parallel**, each in its own git worktree with an explicit file-ownership list and its own
   test database, against those frozen contracts: ingestion pipeline, insights, RAG chat, metrics/rate limits,
   demo corpus + evaluation harness, React dashboard.
3. **The run hit the account usage limit** after 25 minutes; four agents had large uncommitted work. I resumed them
   from their worktrees instead of restarting, and replaced six per-package reviews with one consolidated review
   after merging (cheaper, and it also catches integration bugs).
4. Merged six branches with **zero conflicts** (ownership was disjoint by design); two tests that encoded
   single-branch assumptions were corrected.

## 4. Evaluation as the arbiter

A golden set of 43 cases with known answers (single facts, a table-only figure, multi-document arithmetic,
multi-turn follow-ups, 15 must-refuse questions, prompt-injection probes, Word sections, a public NIST document)
runs against the live API. Its reports exposed three real bugs that every unit test had passed:

| Bug | How the eval showed it | Fix and effect |
|---|---|---|
| Rank-fusion flaw: a chunk found only by an exact identifier ("section 9.2") scored 1/61 and lost to chunks merely okay on both legs | hybrid retrieval was *worse* than either leg alone (MRR 0.891 vs 0.907 / 0.915) | identifier boost + structural phrases; hybrid MRR **0.958**, hit@8 100% |
| Grounding checker false positives on derived numbers ("18 + 2 = 20") and refusals | 3 correct answers flagged "ungrounded", dragging demo answers to "low confidence" | derived values inherit grounding from their calculation; refusals not fact-checked |
| Grounding checker ignored the dates the prompt shows (document dates, today's date), which rule 5 asks the model to use | the correct stipend demo answer had 1–3 "ungrounded" dates and showed Low confidence in 3/3 runs; the pass/fail line stayed green, and a docs-accuracy audit found it in the eval-demo JSON | a shown date counts only right after its cue ("dated", "today" / "as of"); Medium in 3/3 runs after the fix (D-06) |

The same harness chose the answer model: Sol and Luna both passed 43/43; Sol grounded every answer in the comparison
run and produced more high-confidence answers, Luna was 2× faster at p50 and ~20× cheaper with a cold prompt cache →
Sol for answers, Luna for everything else and as the automatic fallback ([docs/DECISIONS.md](docs/DECISIONS.md)).

## 5. Adversarial review before the demo

Four reviewer agents (RAG/chat, pipeline reliability, security/API, frontend/demo UX) each had to prove every
finding with code quotes and live reproductions. 32 verified findings, including one critical one that three
reviewers found independently: concurrent `/health/ready` calls deadlocked Celery's broker connection pool, which
exhausted the API's thread pool so that even DNS lookups for OpenAI connections hung — chat would have failed live
with timeouts. Others: an identifier phrase for "Appendix **A**" collapsing to the bare word "appendix" (the "a" is a
stopword), remote images in model output as a prompt-injection exfiltration path, insights that could get stuck
un-regeneratable, a 500 on non-Latin-1 download filenames. Four fix agents worked in parallel worktrees on disjoint
areas, each adding regression tests.

## 6. Guardrails that made AI-written code trustworthy

- **Frozen contracts + file ownership**: agents could not drift from the schema, SSE contract or each other.
- **CLAUDE.md hard rules**, checked again by reviewers (e.g. a DB session held across a streaming LLM call, unescaped
  document text in a prompt, a status write not fenced by `run_id`).
- **Offline FakeProvider**: ~615 tests run against real Postgres/pgvector with zero API cost.
- **Measure, don't assume**: model latency (TTFT p50/p95), retrieval ranks per case, cost per answer, all from real
  runs; README numbers are copied from reports, with the report id and the prompt-cache state.
- **Honest log**: what went wrong is recorded (usage limit, withdrawn MinIO images, the bugs above), because that is
  where the engineering judgement shows.

## 7. Pre-demo hygiene

Before sharing the repo, a 7-agent workflow checked it, read-only against the live stack:

| Check | Agents | Found | Now |
|---|---|---|---|
| Confidentiality sweep | 2: git history; by location (files, screenshots, eval reports, the live DB) | agents had copied details from my personal configuration into the design-review notes, and an AI-written regression fixture paraphrased the long real-world document (~49k words, not part of the repo) I had tested with | both replaced with neutral text, and the git history rewritten so no commit contains them |
| Clean clone | 1, in an isolated compose project | `make seed` ignored `API=`; host ports were hard-coded | ports overridable (`API_PORT`, `PG_PORT`, `VALKEY_PORT`, `S3_PORT`); seed honours `API` |
| Docs accuracy | 2: auditor + skeptic verifier | the README's cost per answer came from a warm prompt-cache rerun; TTFT included gate refusals (~10 ms, no model call); the date-grounding bug above; a 23 s first follow-up after a restart, hidden by the p95 (the tokenizer downloaded inside the request) | numbers carry report ids and cache state; TTFT over model answers; D-06; tokenizer baked into the image (D-07) |
| Pre-demo cleanup | 2: builder + adversarial reviewer | the first draft's tests used real file names from the live library, and its apply guard could be bypassed | neutral test names; `make pre-demo` applies only the reviewed plan (plan id + counts); `make snapshot-demo` refuses until it passes |

The fixes went through the usual loop (3 builders, 3 adversarial reviewers, 3 fixers) and were squash-merged, so no
branch history carries what review removed. The lesson: AI copies whatever context it sees into artifacts, as the
cleanup tool's first draft showed again. So I sweep artifacts, not only code, before sharing.

## 8. What I did myself

Chose the product and stack trade-offs; wrote and reviewed the contracts; read the core modules (insights
verification, retrieval SQL, rate limiting, the worker attempt logic) and the review findings; ran the end-to-end
smoke and the browser test; decided the answer model from the eval; and learned each module at checkpoints (study
guide with quizzes) so every design decision here is one I can defend.
