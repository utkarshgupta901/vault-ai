# Code review (2026-10-18): `main` at 71be0fa, and what branch `review-fixes` changed

> **Status (2026-10-22): merged.** The fixes below were made on a branch, `review-fixes`, while the OpenAI key was
> revoked. Once a working key was back, the [merge checklist](#merge-checklist) passed on 2026-10-21 and the branch was
> merged into `main`, which is what this repository, the demo video and the final eval run. Today `main` has 1,088
> Python tests and 33 frontend tests, CI is green on GitHub, and the final eval on the merged code is 43/43 (demo
> questions 30/30 runs, hard set 12/13; see [EVAL.md](EVAL.md)). The rest of this document is the review as written
> on 2026-10-18, with the last sections updated.

Five days after the one-day build (2026-10-13), I reviewed the whole repository adversarially and fixed what was
worth fixing, on a branch. `main`, which the demo ran at the time, stayed unchanged until the evals could pass again
with a working OpenAI key (see the [merge checklist](#merge-checklist)).

| | `main` 71be0fa (before) | `review-fixes` (2026-10-18) | `main` today (merged) |
|---|---|---|---|
| Python tests | 618 | 849 collected: 848 pass, 1 skip (the existing one: golden starters not frozen) | 1,088 collected (with the later eval work) |
| Frontend tests | 21 | 33 | 33 |
| CI | none | `.github/workflows/ci.yml` (lint, pytest against service containers, frontend), and `make ci` locally | green on GitHub on every push |
| Commits | | 8 fix-group commits, 7 follow-up commits and this docs commit. Before the docs commit: 85 files, +4,622 / −409 | |

## Verdict

For a one-day build it is well made. The review found nothing critical and no path to another tenant's data. Every
query is filtered by owner, and a test checks that route by route. Worker writes are fenced by run id. Chat retries
are idempotent. Model output is rendered without HTML or images.

The weak spots were operational behaviour and edge cases, the parts a scripted demo does not exercise:

- **Readiness misreported the AI provider.** On 2026-10-17 the API key was revoked and every call returned 401, yet
  `/health/ready` still said `ai: ok`. The watchdog counted a 401 as an answer, and a successful query embedding
  reset the shared failure count between two failed answer calls.
- **Calls held pool connections they did not need.** A file download kept a Postgres pool connection idle in
  transaction for the whole S3 stream. The pool holds 5 + 5 connections, so about 10 slow downloads could stall every
  route for every tenant. Uploads held one the same way across the S3 PUT, and uploads, duplicate requeues and
  reprocesses across the broker publish (up to 5 s).
- **Nothing recovered stuck runs.** A crash between "ready" and the insights publish lost the insights for good. Two
  other cases left a document `queued` or `processing` until someone reprocessed it: a lock wait that gave up, and a
  publish that failed while the broker was down.
- **The cost ledger was wrong on failure paths.** Several billed calls were never recorded: embedding batches of a
  superseded run, a compare whose request was cancelled after the model call, and failed condense, follow-up and
  query-embedding calls the provider still billed. One call was over-recorded: an answer that failed before any
  output was stored as a full-prompt estimate.
- **Answers had edge-case bugs.** An empty model answer was saved as `complete` and replayed on every retry. An
  abbreviation ("vs.") or a range citation (`[S1-3]`) forced a low-confidence badge on a correct answer.
- **The eval could pass vacuously.** If the server stopped sending `signals`, it would still score 43/43, and
  grounding never decided whether a case passed.
- **Local ports and default secrets were exposed.** Compose published Postgres, Valkey and S3 on 0.0.0.0 with the
  repo's public credentials. A prod deploy would have accepted the demo API keys.
- **Some docs overstated.** Cost figures were about 3–6× the measured ones, a threshold described as "calibrated"
  had been set by hand, and the stated quota bound did not cover uploads.

## How it was reviewed

1. **Four adversarial reviewer agents** worked read-only, one per area: security and tenancy; ingestion and workers;
   RAG and chat; frontend, tests, evals and ops. Each finding had to cite file:line and be marked CONFIRMED (traced
   or reproduced) or PLAUSIBLE. Each reviewer also listed what it had tried and refuted. I re-checked the top
   findings by hand.
2. **Static analysis and dependency audits** ran next, and every hit was triaged:

   | Tool | Hits | Verdict | Why |
   |---|---|---|---|
   | `pip-audit` | 0 | clean | no known vulnerabilities in the locked Python dependencies |
   | `pnpm audit` | 0 | clean | no known vulnerabilities in the frontend dependencies |
   | semgrep (owasp-top-ten, python, secrets): `avoid-sqlalchemy-text` | 8, all in `app/services/metrics.py` (e.g. :97, :181, :327, :474) | false positive | The f-strings interpolate only module constants and `_USAGE_GROUP_KEYS[group_by]` (:318-328). `group_by` is a `Literal["operation", "model", "day"]` that FastAPI validates. Every request value (owner id, window) is a bind parameter. |
   | semgrep: md5 | 1, `app/ai/fake_provider.py:36` | false positive | The offline FakeProvider hashes words into vector buckets so its fake embeddings are deterministic. Nothing security-relevant depends on it. |
   | ruff `S105` (hard-coded password) | `app/config.py:38` | real, accepted for local use | `vaultlocalsecret` is the compose S3 password. Since this branch, `APP_ENV=prod` refuses it and the other repo defaults (`app/config.py:117`). |
   | ruff S/B, other hits (e.g. `S311` on `random`) | `app/worker/attempt.py:128` | false positive | `random` only adds jitter to the retry back-off. It never makes a secret or a token. |

3. **Fixes were made in two agent workflows**, all on `review-fixes`, which branches from `main` 71be0fa.
   - **Workflow 1 (22 agents).** Eight fix groups, G1 to G8, owned disjoint sets of files. Each group worked in its
     own worktree with its own test database. In each group:
     - The fixes were written test-first. Each regression test was seen failing before its fix, except the few
       marked "passed before" below.
     - An adversarial verifier re-ran the tests and checked that they fail without the fix.
     - Up to two fix-up rounds followed. G5 needed two rounds and G8 one.
   - **Workflow 2 (8 agents).**
     - Squash-merge the 8 groups. There were no conflicts: of the 76 files, no file was changed by two groups.
     - Run all checks.
     - Run a cross-group review through three lenses: interactions between groups, the 14 CLAUDE.md rules, and
       completeness against the item list.
     - One follow-up fixer.
     - A verifier ran 30 mutation probes, each reverting or breaking one fix; their tests caught all 30. It also ran
       a 60,000-case differential fuzz of the backend and frontend citation grammars and found 0 mismatches.
     - This docs pass, then a final clean check.
4. **Checks on the branch** at c536e24:
   - `ruff check app tests scripts evals` is clean.
   - `pytest`: 848 passed, 1 skipped in 89.8 s.
   - Frontend: `pnpm lint`, `pnpm build` and `pnpm test` (33/33) all pass.
   - The repo's hard rules still hold. There is no new Alembic migration, and `openai` is still imported only in
     `app/ai/openai_provider.py`. Retrieval ranking and the answer prompt's rules text (`answer.v2`) are unchanged.

## Strengths (what held up)

- **Owner-scoped retrieval, tested route by route.** Both retrieval legs filter by `owner_id` and by the document
  ids, and the final join checks `documents.owner_id` again (`app/rag/retrieval.py:42, :54, :77`).
  `tests/security/test_tenancy.py:185` calls every owned route in the OpenAPI spec as another tenant and expects
  404, and `:217` checks that every route needs a key. Reviewers also tried tsquery injection, SPA path traversal,
  cursor tampering and ways around the upload guard (Content-Length vs Transfer-Encoding, path tricks). None worked.
- **Run fencing.** Every worker status write is `UPDATE ... WHERE current_run_id = :run_id`
  (`app/services/state.py:40`), and every write transaction starts with that fenced update. A superseded run
  (reprocess, delete) therefore writes nothing, and a delete waits on the row lock.
- **Idempotent chat.** A retried `client_message_id` gets the saved answer again with no model call. While the
  answer is still streaming it gets 409. After a failure, the answer is regenerated on the same user row
  (`app/services/chat.py:335`).
- **Hardened markdown rendering of model output** (`frontend/src/lib/markdown.ts`):
  - raw HTML is skipped;
  - `<img>` is removed and every `src` is blanked, which closes the markdown-image exfiltration path;
  - links other than http(s) are blanked;
  - `dangerouslySetInnerHTML` is used nowhere.
- **Non-root image** (`Dockerfile:46-47`, uid 10001).
- **Tests on the real stack.** The 618 tests on `main` run against real Postgres + pgvector, Valkey and S3, with the
  offline FakeProvider. SQL, fencing and locks are exercised for real at no API cost.

## Findings and fixes

Severities come from the review. Fix and test locations are on `review-fixes`. Test paths are under `tests/` unless
they start with `frontend/`. Group commits: G1 `ce4e425`, G2 `4e1cfd6`, G3 `0f6db20`, G4 `12b67fc`, G5 `294e9c8`, G6
`ffe02c1`, G7 `519b4e1`, G8 `a743b97`. Follow-up commits: `445e93a`, `84bc57f`, `bea6b8c`, `e450f70`, `b745237`,
`d6beae8`, `c536e24`.

### High

| Finding | Failure scenario | Fix | Regression test(s) | Commit |
|---|---|---|---|---|
| A file download held a Postgres pool connection for the whole S3 stream | FastAPI 0.143 closes yield-dependency sessions only after the response ends. The pool is 5 + 5, so ~10 slow downloads of a 25 MB file leave every connection idle in transaction, and every route for every tenant waits for a slot | `get_file` loads the row in a short `async_session()` and closes it before streaming (`app/api/documents.py:153`). Upload ends its dedup read before the S3 PUT (`app/services/documents.py:130`) | `documents/test_db_connections.py::test_download_streams_without_holding_a_db_connection`, `::test_upload_writes_to_s3_without_holding_a_db_connection` | ffe02c1 |

### Medium

| Finding | Failure scenario | Fix | Regression test(s) | Commit |
|---|---|---|---|---|
| A crash between the ingest's "ready" commit and the insights publish lost the insights for good | The worker is OOM-killed after `_mark_ready` commits. The redelivered ingest cannot claim a `ready` run and exits, so nothing ever publishes insights and they stay `pending` | When an ingest cannot claim its run, it checks for ready + insights pending + the same run and publishes insights again (`app/worker/attempt.py:157`, `:217`). If the broker is down, insights fail as retryable, but only while they are still pending | `ingestion/test_attempt.py::test_redelivered_ingest_publishes_the_insights_its_crashed_run_never_published`, `::test_redelivered_ingest_with_broker_down_marks_insights_failed_retryable`, `::test_redelivered_ingest_with_broker_down_leaves_insights_another_worker_claimed` (test-only, passed before) | 12b67fc, d6beae8 |
| No reaper: runs could stay `queued`/`processing` with no task left (**partly fixed**) | In three cases the message was dropped with only a log line, leaving the document "processing" until a human reprocessed it: a lock wait gave up after 40 re-publishes; a re-publish failed while the broker was down; Celery could not publish a retry | All three now fail the run as retryable (`PROCESSING_TIMEOUT` / `QUEUE_UNAVAILABLE`) under the run fence. They never fail it while an attempt of the same run is `running` (`app/worker/attempt.py:178`, `:199`, `:337`). Lost broker messages still need a reaper (deferred, below) | `ingestion/test_attempt.py::test_lock_wait_that_never_ends_fails_the_run_retryable`, `::test_insights_lock_wait_that_never_ends_fails_insights_only`, `::test_lock_wait_republish_with_broker_down_fails_the_run_retryable`, `::test_retry_that_cannot_be_published_fails_the_run_retryable`, `::test_waiting_duplicate_never_fails_a_run_whose_attempt_is_running` (guards the new behaviour: passed before, mutation-checked) | 12b67fc |
| Transient S3 errors were not retried | During an S3 blip (connection error, read timeout, 5xx, SlowDown) the download fell into the catch-all and the document failed with `PROCESSING_ERROR`, with no automatic retry | `storage.is_transient` classifies these errors and raises `StorageUnavailable` (`app/services/storage.py:26`, `:32`). The worker retries with back-off; when retries run out, the document fails with `STORAGE_ERROR`, retryable (`app/worker/attempt.py:311`). `NoSuchKey`/403 stay permanent | `ingestion/test_storage.py::test_outage_errors_become_storage_unavailable`, `::test_missing_or_forbidden_object_is_not_an_outage`; `ingestion/test_attempt.py::test_storage_outage_during_download_is_retried_then_succeeds`, `::test_storage_outage_that_outlasts_the_retries_fails_retryable` | 12b67fc |
| Billed embedding usage was lost when the run had been superseded | A reprocess or delete during embedding: the batch is billed, the fenced update raises `RunSuperseded` inside the transaction, and the `ai_usage` row rolls back with it | The batch transaction now runs the fence first and writes the vectors only if the run is current. It always writes the usage row, and raises after the commit (`app/ingestion/pipeline.py:209`). A billed but unusable reply gets a `success=false` row (`:236`) | `ingestion/test_pipeline.py::test_embedding_paid_for_by_a_superseded_run_is_still_in_the_ledger`, `::test_billed_embedding_failure_is_in_the_ledger` | 12b67fc |
| An empty model answer was saved as `complete` and replayed | Reasoning uses up all 4,000 output tokens, or the model refuses (refusal events were ignored). The empty stream counted as a success and was saved as `complete`, and every retry with that `client_message_id` replayed nothing | The first token must be visible text (`app/rag/generation.py:67`). With no text, the request falls back to the fast model, then fails with `UPSTREAM_AI_ERROR`. A streamed refusal raises `ProviderError("refusal")` (`app/ai/openai_provider.py:226`). Legacy blank `complete` rows are regenerated, not replayed (`app/services/chat.py:363`), and history skips them (`app/rag/history.py:46`) | `chat/test_ask.py::test_first_attempt_without_answer_text_falls_back[no-text-budget-spent\|whitespace-only\|refusal]`, `::test_no_answer_text_from_either_model_is_failed_not_an_empty_complete`, `::test_a_legacy_empty_complete_answer_is_regenerated_not_replayed`; `chat/test_history.py::test_a_blank_complete_answer_is_not_a_turn`; `insights/test_openai_provider.py::test_a_streamed_refusal_raises_instead_of_ending_empty` | 4e1cfd6, ce4e425, e450f70 |
| `/health/ready` said `ai: ok` while every call returned 401 | With the key revoked (2026-10-17), `_translate` mapped 401 to `bad_request` and the watchdog counted that as a success. In one turn, a successful query embedding between failed answer calls also reset the shared count | A 401/403/404 is now a rejection, remembered per model until that model answers again (`app/ai/__init__.py:41`, `:129`). Readiness reports `ai.ok=false`, `auth_failed=true`, and a generic error that names the path and model, never the vendor text (`app/api/health.py:261`, `:270`). Failures are counted separately for embeddings and for generation. `make doctor` FAILs on `auth_failed` and makes one live embeddings call (`scripts/doctor.py:148`) | `platform/test_provider_watchdog.py::test_an_auth_or_model_rejection_is_sticky_until_that_path_answers[401\|403\|404]`, `::test_an_embedding_success_does_not_hide_failing_text_generation`, `::test_a_text_success_does_not_hide_failing_embeddings`, `::test_another_model_answering_does_not_clear_a_models_rejection`; `platform/test_health.py::test_ai_check_fails_when_either_path_keeps_failing`, `::test_ai_check_stays_red_while_the_answer_model_is_missing_and_turns_answer_on_the_fallback`; `evals/test_doctor_probes.py` (5 tests) | ce4e425, 445e93a |
| The frontend could start two paid answers on a double submit | After a stopped or failed turn, `ask()` checked for a question in flight before an `await` but claimed the slot after it. A double click sent two POSTs and two billed answers, and Stop aborted only one. A session switch inside that window started the old question in the new chat | `askExclusively` claims the slot synchronously, before any await, and re-checks the session after the history refresh (`frontend/src/lib/askSlot.ts:30`; used in `useChat.ts:95`) | `frontend/tests/askSlot.test.ts` (5 tests: one request for two rapid asks; Stop; two session-switch cases; a failed refresh) | 519b4e1 |
| Compose published Postgres, Valkey and S3 on 0.0.0.0 | On a shared network (conference wifi), anyone can read the `vault`/`vault` database or push tasks into the unauthenticated Celery broker | Bound to 127.0.0.1; the API port is unchanged (`compose.yaml:46`, `:56`, `:64`). This takes effect when the containers are recreated | `platform/test_dev_tooling.py::test_stateful_services_are_published_on_loopback_only` | ffe02c1 |
| The eval passed vacuously when the server sent no `signals` | A server regression that dropped or renamed `signals` reads as "0 invalid citations, 0 ungrounded facts", so the run still reports 43/43 | A missing signal is now `None` and fails the check that reads it (`evals/scoring.py:94`, `:108`). The runner fails the run and says why (`evals/run_eval.py:223`) | `evals/test_scoring.py::test_missing_signals_fail_the_checks_that_read_them`, `::test_each_missing_signal_fails_only_its_own_check`; `evals/test_runner.py::test_an_answer_without_signals_fails_and_says_why` | a743b97 |

### Low

| Finding | Failure scenario | Fix | Regression test(s) | Commit |
|---|---|---|---|---|
| `/health/ready` echoed raw exception text, and its DB probe was not single-flight | The endpoint needs no key, and `str(exc)` can name a host or user ("password authentication failed for user ..."). 20 concurrent calls ran 20 DB probes | The DB probe is single-flight with a 2 s TTL (`app/api/health.py:161`, `:254`). Public errors are generic: "unreachable" only for connection errors, otherwise "check failed (see the API log)" or "timed out after 2s". The detail is logged (`:64-70`, `:227`) | `platform/test_health.py::test_concurrent_readiness_calls_share_one_db_probe`, `::test_ready_does_not_echo_dependency_errors`, `::test_only_a_dependency_that_could_not_be_reached_is_called_unreachable`, and four tests of the cache itself | ce4e425, 445e93a |
| Float pricing lost half-millionths, and an unknown model cost $0 silently | 10 gpt-5-nano input tokens were recorded as $0.000000 instead of $0.000001. A model missing from the price table under-counts both spend and the quota with no warning | Prices are `Decimal`, rounded half-up to the ledger's 6 places. An unknown model is still $0 but logs `ai_price_unknown` once (`app/ai/pricing.py:42`) | `platform/test_pricing.py` (3 tests) | ce4e425 |
| Embeddings were not asked for `EMBED_DIMS` | `EMBED_MODEL=text-embedding-3-large` returns 3,072 dimensions. The call is billed, then the `vector(1536)` write fails the document and loses the usage row | Pass `dimensions=EMBED_DIMS` to text-embedding-3 models, and reject a wrong-size reply with its usage attached (`app/ai/openai_provider.py:117`, `:160`) | `insights/test_openai_provider.py::test_embeddings_ask_for_the_stored_dimensions`, `::test_older_embedding_models_get_no_dimensions_parameter`, `::test_embedding_of_the_wrong_size_is_rejected_with_usage` | ce4e425 |
| Compare wrote its usage row only after post-processing | A client disconnect, or a post-processing bug, after the paid call meant no `ai_usage` row (rule 7) | The row is written right after the call, in a shielded scope (`app/ai/compare.py:189`) | `insights/test_compare_usage.py` (2 tests) | ce4e425 |
| Vendor error text reached clients and tenants | A 401's vendor message (it echoes part of the key) went into the SSE `error` event, the JSON 502 and `processing_runs`, which `/metrics/processing` serves | Clients get a generic message, and the vendor text goes to the log (`app/services/chat.py:582`). `processing_runs` stores only the kind and status, e.g. `bad_request (HTTP 401)` (`app/ai/provider.py:75`, `app/worker/attempt.py:304`, `app/ai/insights.py:238`) | `chat/test_ask.py::test_provider_error_text_never_reaches_the_client`; `ingestion/test_attempt.py::test_vendor_error_text_stays_out_of_the_processing_metrics[ingest\|insights]` | 4e1cfd6, d6beae8 |
| A complete answer could be saved twice, or saved as `interrupted` | A disconnect at the `citations` yield (before the save), or a cancel while the final COMMIT is in flight, left `run.finished` false. The `finally` then re-saved the turn as `interrupted` and inserted its `ai_usage` rows a second time | The answer is saved before `citations` is yielded. Every `_save` runs in a shielded scope and sets `finished` before any await (`app/services/chat.py:491`, `:712`). The JSON deadline uses `anyio.fail_after` so it respects the shield (`:817`) | `chat/test_ask.py::test_disconnect_at_the_citations_event_keeps_the_answer_complete`, `::test_cancel_right_after_the_final_commit_does_not_save_the_turn_twice`, `::test_json_deadline_during_the_final_save_waits_for_it`, `::test_cancel_while_the_failed_save_commits_does_not_save_the_turn_twice` | 4e1cfd6, e450f70 |
| A failed answer was billed as a full-prompt estimate | When both models fail before the first token with a 5xx or connection error, the ledger recorded `count_tokens(prompt)` as estimated input, which counted against the daily quota | Zero tokens, or the provider's own usage when the error carries it (`app/services/chat.py:579`) | `chat/test_ask.py::test_both_models_failing_before_the_first_token_is_an_error_billed_at_zero` | 4e1cfd6 |
| The JSON endpoint could return 504 for an answer it had saved | A long answer plus follow-ups (up to 8 s) ran past the 45 s deadline after `_save("complete")` | On a timeout, return the saved answer without chips if it is complete (`app/services/chat.py:847`) | `chat/test_ask.py::test_json_deadline_during_followups_returns_the_saved_answer` | 4e1cfd6 |
| Failed turns crowded real turns out of the history window | `LIMIT 6` was applied to rows, so the questions of failed or interrupted turns took the slots of answered ones | History selects (question, complete reply) pairs, newest 3 (`app/rag/history.py:34`). The reply is matched on the session too, so the query uses the `(session_id, seq)` index instead of scanning every owner's messages | `chat/test_history.py::test_failed_and_unanswered_turns_do_not_crowd_out_complete_ones`, `::test_a_regenerated_turn_counts_once_with_its_complete_answer`, `::test_the_history_query_reads_only_this_sessions_messages` (EXPLAIN with seq scans off) | 4e1cfd6, e450f70 |
| History text bypassed `escape()` (rule 5) | A past answer echoing injected document text, or a past question, could forge a `<sources>` block ahead of the real one | History is escaped with `quote=False`: `<`, `>` and `&` are escaped, and quotes stay as typed so the model does not see `don&#x27;t` (`app/ai/prompts/answer.py:105`, `app/ai/prompts/common.py:18`) | `chat/test_answer_prompt.py::test_history_text_is_escaped_and_keeps_its_roles`, `::test_history_keeps_apostrophes_and_quotes_as_typed` | 4e1cfd6, e450f70 |
| The sentence splitter broke on abbreviations | In "up to 18 days vs. 10 days [S1].", "up to 18 days vs." was counted as an uncited fact, which forced a low badge on a correct, cited answer | No split after vs., e.g., i.e., approx., etc., No., Inc., Ltd., Dr., Mr., Ms., cf. (`app/rag/citations.py:51`) | `chat/test_citation_parsing.py::test_vs_abbreviation_does_not_force_a_low_badge`, `::test_common_abbreviations_are_not_sentence_ends`, `::test_real_sentence_boundaries_still_split` (passed before) | 0f6db20 |
| Range and lower-case citations were not parsed | For `[S1-3]` or `[s1]` the sentence counted as uncited (low badge). In history, `[S12-14]` leaked a fake fact "14" into the grounding check | The citation grammar accepts ranges with six kinds of dash, lower case and `;`. Ranges expand up to 20 ids; a wider or reversed range keeps only its two ends (`app/rag/citations.py:43-45`, `:106`). The frontend mirrors the grammar, so every marker the backend counts is drawn as a chip (`frontend/src/lib/citations.ts:21`) | `chat/test_citation_parsing.py` (6 range tests, incl. `::test_ranges_written_with_any_hyphen_are_parsed_and_stripped`); `frontend/tests/citations.test.ts` (4 new cases) | 0f6db20, 84bc57f |
| Docstrings overstated costs and calibration | `confidence.py` said an answer costs ~$0.015 and `ratelimit.py` ~$0.03; the measured cost is $0.0042–0.0053 (EVAL.md). `TAU_HIGH` was called "calibrated", but it was set by hand. The `/search` docstring left out the identifier boost | Docstrings corrected (`app/rag/confidence.py:5-17`, `app/core/ratelimit.py:16`, `app/api/search.py`) | none (docs only) | 0f6db20 |
| `explain_retrieval.py` re-implemented the ranking and printed MISMATCH | The script left out the identifier boost, so every identifier hit printed "MISMATCH" against `/search` | `retrieval.id_boost()` is shared by `retrieve()` and the script (`app/rag/retrieval.py:134`). The values are identical, so the ranking is unchanged | `chat/test_explain_retrieval.py::test_script_ranking_equals_retrieve`, `::test_script_reuses_the_boost_of_retrieve` | 0f6db20, c536e24 |
| A truncated value in the query-embedding cache disabled vector search for that question | Found by the new cache tests: a stored value that is not a whole number of float32s made `frombytes` raise. The question then ran keyword-only until the entry expired (24 h) | The byte length is checked before unpacking, so such a value is a miss (`app/rag/cache.py:43`) | `chat/test_embed_cache.py` (5 tests; only the wrong-length case failed before) | 0f6db20 |
| A delete during ingest was recorded as `STORAGE_ERROR` | A delete removes the row, then the object. The worker's download got `NoSuchKey`, and `/metrics/processing` counted a storage failure | On a 404, if the document or its run is gone, the attempt is `SUPERSEDED` (`app/ingestion/pipeline.py:112`) | `ingestion/test_pipeline.py::test_document_deleted_during_download_is_superseded_not_a_storage_error` | 12b67fc |
| `TRANSITIONS` was dead data | The state-machine maps were referenced nowhere and could drift from the statements | A test runs every `*_stmt` from all 16 states and requires exactly the documented edges. It found a missing `pending -> pending` | `ingestion/test_state_transitions.py` (2 tests) | 12b67fc |
| The upload sniffer read whole files into memory | Up to 25 MB per upload was held at once just to check UTF-8 | The check streams 64 KB chunks through an incremental decoder (`app/ingestion/sniff.py:65`) | `ingestion/test_sniff.py` (4 text tests) | 294e9c8 |
| DOCX limits did not bound the worker's memory | A ~2 MB zip whose `word/document.xml` inflates to ~190 MB passed the 200 MB total and the ratio check. python-docx then builds an lxml tree several times that size and can OOM the 512 MB worker | Per-entry cap of 30 MB and a total of 64 MB (`app/ingestion/sniff.py:27`, `:33`). Before python-docx parses anything, the worker counts XML nodes (elements, attributes, comments, PIs, namespace declarations; limit 500k) and re-checks the entry sizes (`app/ingestion/extract.py:65`, `:329`). A python-docx `RecursionError` counts as `CORRUPT_FILE`, not as retryable (`:73`) | `ingestion/test_sniff.py::test_one_huge_entry_is_unsafe_even_when_total_and_ratio_are_within_limits`; `ingestion/test_extract.py` (8 DOCX tests, incl. `::test_docx_of_millions_of_comments_pis_or_namespace_declarations_is_refused_too`, `::test_docx_over_the_upload_caps_is_refused_before_any_entry_is_read`, `::test_docx_that_overflows_the_python_stack_is_corrupt_not_retryable`) | 294e9c8, d6beae8 |
| TXT, MD and DOCX had no size cap | A 25 MB text file is ~6M tokens and ~15k chunks: embedding it hits the 540 s soft limit (`PROCESSING_TIMEOUT`) on every reprocess | New permanent error `TOO_MUCH_TEXT`, above `MAX_PAGES` × 5,000 characters or × 1,250 tokens. Tokens are counted in 64k-character slices, stopping once over the limit (`app/ingestion/extract.py:51`, `:118`; `app/core/errors.py:70`). PDFs keep `TOO_MANY_PAGES` | `ingestion/test_extract.py::test_non_pdf_with_more_text_than_max_pages_worth_is_too_much_text`, `::test_token_dense_text_under_the_character_limit_is_still_too_much_text`, `::test_token_limit_is_counted_in_bounded_slices_and_stops_once_over` | 294e9c8 |
| PDFs with only tiny text were rejected as scanned | A one-page "Invoice #1234 paid" (18 characters) got `NO_EXTRACTABLE_TEXT` | A PDF is rejected as scanned when no page has text, or when several pages have only short stamps (`app/ingestion/extract.py:160`) | `ingestion/test_extract.py::test_pdf_whose_only_text_is_short_is_not_mistaken_for_a_scan`, `::test_short_text_pdf_warns_only_about_its_blank_pages`, `::test_scan_whose_only_text_is_stamped_page_or_bates_numbers_is_no_extractable_text` | 294e9c8 |
| Dead optional-router imports, and a phantom upload path | A broken router import silently dropped its routes. `/documents/batch`, which does not exist, got the upload guard | Direct imports, so a broken import fails at startup (`app/main.py:97`). The guard covers only `POST /api/v1/documents` (`app/core/upload_guard.py:25`) | `platform/test_upload_guard.py::test_only_the_real_upload_route_gets_the_upload_limit` | ffe02c1 |
| The request-log buffer was never flushed on shutdown | Up to 1 s of `request_logs` rows was lost on every restart | The lifespan awaits `aclose()` before it disposes of the engine (`app/main.py:79`) | `observability/test_request_log.py::test_app_shutdown_writes_the_rows_still_buffered` | ffe02c1 |
| Seed keys and S3 defaults were accepted in prod | A Render deploy with one `SEED_*` value left unset would seed `vault-demo-key`, the dashboard's default key, as a working prod key | With `APP_ENV=prod`, settings refuse empty or repo-default S3 keys and any seed key the process was given (`app/config.py:117`). The bootstrap refuses to seed public keys (`app/cli.py:62`). Validation errors no longer echo secrets (`hide_input_in_errors`) | `platform/test_prod_settings.py` (6 tests) | ffe02c1 |
| `app/cli.py` used `print` (rule 10) | The deploy log mixed plain prints with structured lines | structlog events, carrying names only, never keys | `platform/test_cli_logging.py` (2 tests) | ffe02c1 |
| The duplicate-upload requeue was not fenced, and `upload_count` lost updates | Two duplicate uploads of a failed document both requeued it, replacing the winner's run. Concurrent duplicates counted 2 instead of 3 | The requeue repeats "failed and retryable" in its WHERE clause, and the count is incremented in SQL (`app/services/documents.py:167`) | `documents/test_duplicate_upload.py` (3 tests) | ffe02c1 |
| A NUL in a filename gave a 500 and an orphaned S3 object | Postgres refuses NUL in TEXT. Only the `IntegrityError` branch deleted the object it had just uploaded | `clean_filename` replaces control characters and turns a blank, `.` or `..` name into `upload` (`app/services/documents.py:56`). Any failure after the PUT deletes the object, after a rollback, except when the COMMIT's outcome is unknown | `documents/test_api_hardening.py::test_clean_filename`, `::test_control_characters_in_the_filename_are_replaced`, `::test_a_failure_after_the_s3_put_deletes_the_object`, `::test_a_connection_dropped_during_commit_keeps_the_object`; `documents/test_db_connections.py::test_an_orphan_is_deleted_without_holding_a_db_connection` | ffe02c1, b745237 |
| Document-event streams reconnected every 3 s forever | With a revoked key, every followed document sent a request that could only fail, every 3 s, for as long as the page stayed open | Back-off doubles from 3 s up to 60 s. On 401/403/404 the page stops following that document (`frontend/src/lib/reconnect.ts:21`) | `frontend/tests/reconnect.test.ts` (3 tests) | 519b4e1 |
| The headline numbers hid what they measured | "43/43" was one sample per case. The report had no max latency, so p95 hid a 23.4 s outlier (D-07). TTFT was measured with every query embedding cached | The headline is labelled "first-run", with `passed_all_runs` next to it when cases repeat. Latency tables show the max. Reports and EVAL.md carry a TTFT caveat about cached embeddings | `evals/test_runner.py::test_full_run_scores_everything`, `::test_ttft_caveat_only_cites_a_retrieval_phase_that_ran`; `evals/test_scoring.py::test_percentile_is_nearest_rank` | a743b97 |
| Grounding never decided a case | No case set `expect.grounded: true`, so an ungrounded number was reported but never failed the case | All 28 answerable cases gate on grounding. Re-scoring the stored 2026-10-14 answers offline: 43/43 and 30/30 runs pass; a live re-run is pending | `evals/test_golden.py::test_every_answerable_case_gates_on_grounding` | a743b97 |
| No CI | Nothing ran the suite on push | `.github/workflows/ci.yml`: ruff; pytest against pgvector, Valkey and RustFS service containers on compose's ports; frontend lint, build and test. `make ci` runs the same steps locally. The tokenizer cache lives outside the checkout | `platform/test_dev_tooling.py::test_ci_keeps_the_tokenizer_cache_outside_the_checkout` | a743b97, c536e24 |

### Found by the cross-group review (all low, fixed in the follow-up commits)

The group fixes were each correct on their own, but some of them met at a boundary between groups. A few also
stopped one step short.

| ID | Finding | Failure scenario | Fix | Test | Commit |
|---|---|---|---|---|---|
| X1 | Readiness tracked rejections per path, so the fallback model's success hid a missing answer model | `ANSWER_MODEL` returns 404 and chat answers on the fast model. That success cleared the path's rejection, so readiness went green while every answer took the fallback | Rejections are kept per model within a path. The error names the model (`app/ai/__init__.py:47`, `app/api/health.py:261`) | `platform/test_provider_watchdog.py::test_another_model_answering_does_not_clear_a_models_rejection`; `platform/test_health.py::test_ai_check_stays_red_while_the_answer_model_is_missing_and_turns_answer_on_the_fallback` | 445e93a |
| C12 | Readiness called every failure "unreachable" | A missing table or a rejected Valkey password read as a network problem | "unreachable" only for connection errors, otherwise "check failed (see the API log)" (`app/api/health.py:64-70`) | `platform/test_health.py::test_only_a_dependency_that_could_not_be_reached_is_called_unreachable` | 445e93a |
| X2, X3 (C01, C02) | The backend counted citation forms the UI drew as plain text | An answer citing `[S1-3]` or `[s1]` got a high badge, while the UI showed the raw marker. Ranges written with U+2010, U+2011 or U+2212 were not parsed at all | `frontend/src/lib/citations.ts` mirrors the backend grammar and the 20-id range cap. Both sides accept the same six dashes | `chat/test_citation_parsing.py::test_ranges_written_with_any_hyphen_are_parsed_and_stripped`; `frontend/tests/citations.test.ts` | 84bc57f |
| X4, X5, R5 (C03) | A billed failure of a query embedding, condense or follow-ups call was recorded at 0 tokens | A wrong-size embedding, or a refused or unparsable condense call, was billed, but the ledger row said 0 | Keep `exc.usage` on failure (`app/rag/cache.py:90`, `app/rag/condense.py:67`, `:76`, `app/rag/followups.py:85`) | `chat/test_embed_cache.py::test_a_billed_but_unusable_embedding_keeps_its_usage`; `chat/test_unit_rag.py` (3 tests) | bea6b8c |
| R2 (C05) | The `failed` save was not shielded | A disconnect during the failed save's COMMIT re-saved the turn as `interrupted` and duplicated its usage row | The shield moved inside `_save`, so it covers every status (`app/services/chat.py:712`) | `chat/test_ask.py::test_cancel_while_the_failed_save_commits_does_not_save_the_turn_twice` | e450f70 |
| C04 | The new history query scanned all of `chat_messages` | No index covers `reply_to_id`, so every turn read every owner's messages | `reply.session_id == session_id` (`app/rag/history.py:46`) | `chat/test_history.py::test_the_history_query_reads_only_this_sessions_messages` | e450f70 |
| C06 | Empty `complete` rows saved before the fix were still replayed | A retry of such a turn kept getting nothing | Regenerate them, and skip them in history (`app/services/chat.py:363`, `app/rag/history.py:46`) | `chat/test_ask.py::test_a_legacy_empty_complete_answer_is_regenerated_not_replayed`; `chat/test_history.py::test_a_blank_complete_answer_is_not_a_turn` | e450f70 |
| R1 | Escaped history showed the model `don&#x27;t` | The model could copy entity text into new answers in front of users | `escape(quote=False)` for history (`app/ai/prompts/answer.py:105`) | `chat/test_answer_prompt.py::test_history_keeps_apostrophes_and_quotes_as_typed` | e450f70 |
| R3 (C08) | Upload, duplicate requeue and reprocess held a pool connection during the broker publish | `db.refresh` opened a read transaction that stayed idle for up to the 5 s publish timeout | `await db.commit()` first in both enqueue helpers (`app/services/documents.py:97`, `:248`) | `documents/test_db_connections.py::test_the_broker_publish_holds_no_db_connection` (4 paths) | b745237 |
| R4 | The orphan cleanup deleted from S3 while the failed session still held its connection | One pool connection was held per failed upload during the S3 DELETE | Roll back first | `documents/test_db_connections.py::test_an_orphan_is_deleted_without_holding_a_db_connection` | b745237 |
| C11 | `clean_filename` returned blank names | `/`, spaces or `..` listed as a blank row | Becomes `upload` (`app/services/documents.py:56`) | `documents/test_api_hardening.py::test_clean_filename` | b745237 |
| C07 | Vendor text still reached tenants through `/metrics/processing` | While the key is dead, every failed run stores the 401's vendor message | `ProviderError.summary()` is stored, and the vendor text is only logged | `ingestion/test_attempt.py::test_vendor_error_text_stays_out_of_the_processing_metrics[ingest\|insights]` | d6beae8 |
| C09 | The worker read DOCX entries without checking their declared size | A DOCX stored under the older, looser caps can still be reprocessed | Entry sizes are checked against the upload caps before any read (`app/ingestion/extract.py:329`) | `ingestion/test_extract.py::test_docx_over_the_upload_caps_is_refused_before_any_entry_is_read[entry\|total]` | d6beae8 |
| C10 | A python-docx `RecursionError` was retried | A long vertical-merge chain overflowed the stack after ~27 s, then went down the retry path | Counted as `CORRUPT_FILE` (`app/ingestion/extract.py:73`) | `ingestion/test_extract.py::test_docx_that_overflows_the_python_stack_is_corrupt_not_retryable` | d6beae8 |
| R6 (C14) | No test pinned the "only while still pending" guard | A mutation that removed the guard survived | Test only; it fails when the guard is removed | `ingestion/test_attempt.py::test_redelivered_ingest_with_broker_down_leaves_insights_another_worker_claimed` | d6beae8 |
| C13 | `explain_retrieval.py` still copied the boost formula | The formula could drift from `retrieve()` | Shared `id_boost()` (`app/rag/retrieval.py:134`, `scripts/explain_retrieval.py:34`) | `chat/test_explain_retrieval.py::test_script_reuses_the_boost_of_retrieve` | c536e24 |
| C15 | CI kept the tokenizer cache inside the checkout | Every CI checkout would have had an untracked `.cache/` | `/tmp/tiktoken-cache` (`runner.temp` is not available in a job-level `env`) | `platform/test_dev_tooling.py::test_ci_keeps_the_tokenizer_cache_outside_the_checkout` | c536e24 |
| X7, D01–D10 | Docs the branch made stale | README, DEPLOY_RENDER, PROMPTS, AI_USAGE, Makefile help and four docstrings (`state.is_insights_stale`, `_republish_lost_insights`, doctor, the health error classes) | Updated in the docs commit that adds this file | none | this commit |

## Deliberate non-changes

- **A delete keeps the filename in `chat_messages.retrieval` (G6.9, skipped).**
  - **Why.** This is documented policy: a delete redacts cited snippets but keeps the filename in citations and
    retrieval. Other code depends on it:
    - `replay_events` rebuilds sources from the citation's filename and label, which are required strings.
    - The pre-demo tool uses the retrieval filename of an already-deleted document to tell corpus files from other
      files (`evals/demo_library.py`).
    - Redacting retrieval alone would not hide the name, because the citations keep it. Nulling it would make
      replays fail with a 500.
  - **Next step.** This needs a product decision. If erasure-grade delete is wanted, redact citations and retrieval
    together to a placeholder string such as "(deleted document)", and update replay, `gone_names`/`plan_session`
    and the fake API in the same change.
- **The answer prompt version was not bumped for escaped history (R1, F07).**
  - **Why.** The rules text is unchanged, and the history now differs from `main` only where a past answer contains
    `<`, `>` or `&`. A bump would ripple into stored reports and four docs.
  - **Next step.** In the live re-run, check the multi-turn answers for entity text (`&amp;`, `&lt;`).
- **The cleanup save is not fenced by status (C05, optional part).** The shield inside `_save` closes the reproduced
  double save. A `WHERE status = 'streaming'` fence would change `_save`'s contract for every status.
- **Refusals and empty streams count as watchdog successes (X8).** The watchdog measures whether the client and the
  provider answer, not answer quality. Counting these as failures would trigger client resets that cannot help.
- **No test for `.distinct(question.seq)` in history (R7).** It guards a state the code cannot produce: two complete
  replies to one question.

## Deferred

### Tier 3 (out of this branch's scope from the start)

| Item | What is still possible today | Next step |
|---|---|---|
| Reaper / beat sweep for stuck runs | The code no longer leaves a run stuck on its own paths. Two causes remain. A message lost by the broker: Render's free Key Value has no persistence, and Valkey can be flushed. A worker killed at the hard time limit: its message is acked, so insights stay `processing`. Those documents show `stale` after 15 minutes and need a manual reprocess | A Celery beat task every 5 minutes that finds `IN_FLIGHT` rows idle past `STALE_PROCESSING_MIN` (and the same for insights) and fails them as retryable under the run fence. Test it with a backdated `updated_at` |
| Worker-side quota and a hard reservation quota | The quota is checked when an upload or reprocess is accepted, and the worker records its cost later. A burst of large uploads can therefore pass the check and overshoot; only the per-minute limit caps it | The worker checks `today_spend_usd` before the embed and insights stages and fails with `QUOTA_EXCEEDED`. Later: reserve an estimated cost at request time and settle it afterwards |
| `request_logs` retention and unauthenticated noise | Every keyless `/api/*` request writes a row, nothing prunes the table, and the rows feed `/metrics/system` | Log only requests that reached auth (or aggregate the rest in memory), and add a daily retention delete (or partitioning by day, as in the README's scale table) |
| Per-user caps on SSE and read routes | Each `/documents/{id}/events` stream polls the DB every 500 ms for up to 10 minutes, with no per-user cap | Add `rate_limit("read")` on the read routes, and a per-user count of concurrent SSE streams in Valkey |
| Stream heartbeat instead of the 90 s stale expiry | A legitimate answer stream that runs over `STREAMING_STALE_S` (90 s) can be expired, and a second ask then starts a second answer | Give streams an overall deadline below 90 s, or a heartbeat column that the stale check reads |
| Identifier tokens that bypass the gate | An off-topic question with a letters+digits token that appears in some chunk ("q3", "5g") skips the similarity gate and pays for an answer | Require the identifier hit to rank in the top k, or keep a similarity floor even with a hit. Add an off-topic golden case containing such a token |
| Unicode lexical terms | The keyword leg is ASCII-only: "café" becomes "caf", and a non-Latin query has no keyword terms | Use `[^\W_]+` with Unicode-aware stopwords, drop one-character terms, and add a retrieval case for each |
| Eval negation, pass@N, cold-embedding TTFT | Substring matching passes "not 18 days" for "18 days". Each non-demo case runs once. TTFT is measured with query embeddings cached | Add `forbid` negations to the single-token cases, `--repeat N` for every case with pass@N, and a TTFT run on a stack with an empty query cache (never the demo stack) |
| Render worker deploy ordering | Only the API runs the pre-deploy migration, so the worker can start new code on the old schema | Make the worker wait for the Alembic head at startup, or deploy it after the API |
| Image digest pinning and `forwarded-allow-ips` | Base images float on their tags, and the API trusts `X-Forwarded-For` from any peer (`--forwarded-allow-ips=*`) | Pin `node` and `python` by digest. Set `forwarded-allow-ips` to the proxy's address, and confirm no client IP feeds a rate limit |

### Lows the review deferred

| Item | What is still possible today | Next step |
|---|---|---|
| An S3 outage during upload or download is a 500 (X6) | `put_file` or `open_stream` raising `EndpointConnectionError` reaches the client as `INTERNAL`. This existed before the branch; G4's transient classification is used only in the worker | Add an API error code (e.g. `STORAGE_UNAVAILABLE`), and answer 503 with `Retry-After` when `storage.is_transient(exc)`. Test with `put_file` patched to raise |
| Narrow races in the G4.5 lock-wait give-up (F03) | It needs about 10 minutes of lock contention. `_fail_waiting_run` checks and then fails without a lock, and `lock_waits` is carried into `task.retry` copies. The outcome is a retryable failure, never data loss | Fold the "no running attempt" guard into the UPDATE, try the advisory lock once more before failing, and reset `lock_waits` on retry |
| Blind spots in the scan heuristic (F04) | A multi-page PDF whose pages all hold short real text is refused as scanned. A one-page scan whose only text is a stamp is indexed | Add a total-characters floor next to the "more than one page" rule. This needs real samples |
| `MAX_XML_NODES` wording (F05) | A very large table-heavy DOCX (over 500k XML nodes) gets `UNSAFE_ARCHIVE`, which is misleading for a benign file | Use a "too large or complex" message, and revisit the cap with real Word files |
| Frontend busy state and hook tests (F06) | A different question sent during the brief history refresh after a stopped turn is dropped. The hook wiring is covered only by tsc and lint | Expose an `asking` flag from `useChat` to the Composer, and add a React hook test setup |
| Gaps in readiness wording (verifier on C12) | psycopg reports a rejected Postgres password or a missing database as `OperationalError`, which still reads "unreachable". A redis `TimeoutError` reads "check failed". `storage.ping` swallows every error, so wrong S3 credentials read "bucket not reachable" | Classify by `exc.orig.sqlstate` (classes 28 and 3D: reached), add redis `TimeoutError` to the unreachable errors, and let `ping` re-raise non-connection `ClientError`s |
| Timed-out condense, follow-up and query-embedding calls record 0 tokens | This existed before the branch: a call that hits its own timeout was usually billed (rule 7) | Record `estimated_usage(...)` (`estimated=true`) on a timeout, as the cancellation path already does |
| Unicode parity of the citation grammars | Python's `\d`, `\s` and `IGNORECASE` are Unicode-aware, and the JS regex is not, so `[S１]` (fullwidth digit) counts on one side only. A 60,000-case fuzz over realistic input found no mismatch | Use explicit `[0-9]` and a fixed whitespace class on both sides |
| A shield does not stop a native `task.cancel()` (N01) | Request-path cancellations come from anyio, so the shields hold. A hard shutdown can still cut a ledger write | If shutdown durability matters, run ledger writes in an `asyncio.shield`ed task |
| ~~CI has not run on GitHub~~ Done | Green on GitHub since 2026-10-18 (runs 39141265270 and 39645144891 on the branch, and on every push to `main` since the merge) | — |

## Verified once the key worked (2026-10-21)

When this review was written the OpenAI key had been revoked since 2026-10-17, so nothing above had run against the
real API; every test used the FakeProvider and a mock transport. With a working key, everything that list named was
checked on 2026-10-21 against a second compose stack (API on port 8001) built from the branch:

- **`make eval-demo`** found one more bug: the first run on 330610e was 29/30, because the grounding checker derived
  only the result after the last `=` in a sentence and flagged a correct two-calculation answer. The fix (`1e534b3`,
  with tests) made it 30/30 runs, and **the full `make eval`** passed 43/43.
- **`make doctor`**: 19 PASS, 0 FAIL, including the real embeddings call it now makes.
- **Escaped history** in multi-turn answers: the 6 multi-turn cases pass in every run, and no answer in the final
  reports contains an escaped entity such as `&amp;` or `&lt;`.
- **Loopback ports**: after `make demo` recreated the demo stack from the merged code, Postgres, Valkey and S3 are
  published on `127.0.0.1` only.
- **CI**: green on GitHub.
- **Not exercised by a live run:** a streamed refusal or an empty stream from the real Responses API (none happened);
  the fallback they trigger is covered by tests against the mock transport. `make smoke-llm` was not re-run
  separately; the evals called every configured model.

After the merge, the final eval on the merged code (`ea90635`) passed 43/43 with every answer grounded, the demo
questions passed 30/30 runs, and a harder 13-case set passed 12/13 (reports `20261021T205542Z`, `20261021T205852Z`).
The demo stack rebuilt from `main` passed 30/30 runs again (`20261021T210636Z`).

## Merge checklist

All green on 2026-10-21; `review-fixes` was merged into `main` on 2026-10-22.

1. **Baseline.** With a working key, on `main` as it ran then (71be0fa), `make eval-demo`: 10/10 cases, 30/30 runs
   (`20261021T164121Z`). The full eval on that `main` was 42/43: one correct answer worded outside the expected
   phrases (`20261021T163824Z`), which the later phrase matcher accepts.
2. **On `review-fixes`**, on a second stack built from the branch:
   - `make demo`: rebuilt from the branch (loopback-only ports confirmed on the demo stack after the merge);
   - `make doctor`: 19 PASS, 0 FAIL, with a real embeddings call;
   - `make eval-demo`: 30/30 runs after the grounding fix in `1e534b3`;
   - the full `make eval`: 43/43; the multi-turn answers show no escaped entities.
3. **Merged** only after every step was green, CI included.
