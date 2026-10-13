# Chat with your documents: RAG best practice for 2025-2026, scoped to a 1-day build

Facts were checked on 2026-10-13 against the linked sources. **Build** means minutes with Claude Code writing the code, including one manual test. **Explain** is a 1-2 sentence answer for the interview panel. Anything I could not confirm from a primary source is marked **UNVERIFIED**.

## 0. Reference pipeline

```
INGEST (Celery task; idempotent on sha256(file bytes)+owner)
  extract text per page → page-aware recursive split (~400 tok, ~15% overlap)
  → prepend header "Title > Section (p.N)" → batch-embed (hash cache) → INSERT chunks
  → ONE structured-output LLM call: summary, key points(+pages), category, tags, sentiment, starter questions
QUERY (per chat turn)
  history? → condense to a standalone question (cheap LLM)
  → hybrid SQL: pgvector top-40 ∪ Postgres FTS top-40 → RRF(k=60) → per-doc cap → top 8
  → relevance gate (refuse early, no LLM call)
  → prompt = system rules + last 3 turns + <sources>[1..8] (fenced, untrusted) + question last
  → stream answer (SSE) → parse [n] → validate → citations {doc, pages, snippet}
  → follow-up suggestions; persist message + tokens + cost + per-stage timings
```

---

## 1. Chunking

### 1a. Page-aware recursive splitting at ~400 tokens with ~10-15% overlap (DO)
- **What:** Extract text one page at a time (pypdf or pdfplumber). For DOCX/Markdown, use one heading section at a time. Split each with `RecursiveCharacterTextSplitter.from_tiktoken_encoder(chunk_size=400, chunk_overlap=60)` from `langchain-text-splitters`. Store `chunk_index`, `page_start`, `page_end`, `heading_path`, `token_count`, and the raw `content`. Merge tail chunks under ~50 tokens into the previous chunk.
- **Why:**
  - Chroma's token-level study found that a plain recursive splitter at 200-400 tokens performs well. It also found that OpenAI's old 800/400 default gave "the lowest scores across all other metrics" apart from recall, and that removing overlap improved precision and IoU ([Chroma](https://trychroma.com/research/evaluating-chunking)).
  - NVIDIA found page-level chunking best on average (0.648 accuracy, lowest variance, std 0.107). Among token-based sizes, 15% overlap was best. Factoid queries did best at 256-512 tokens and analytical queries at around 1024 ([NVIDIA](https://developer.nvidia.com/blog/finding-the-best-chunking-strategy-for-accurate-ai-responses/)).
  - Splitting inside page boundaries means every chunk has an exact page number, so citations come for free.
- **Build:** 20-25 min.
- **Explain:** "I chunk inside page and section boundaries at about 400 tokens with a little overlap. Published benchmarks put the sweet spot at a few hundred tokens, and because chunks never cross a page, every citation points to a real page."

### 1b. Parent-child (small-to-big) retrieval (STRETCH, cheap variant only)
- **What:** Embed small child chunks, but give the LLM the larger parent (section or page). This is LangChain's ParentDocumentRetriever, or LlamaIndex's HierarchicalNodeParser plus AutoMergingRetriever ([LlamaIndex](https://developers.llamaindex.ai/python/framework/integrations/retrievers/auto_merging_retriever/)). The 1-day variant is "neighbor expansion": also fetch `chunk_index ± 1` from the same document.
- **Why:** It separates retrieval precision (small units) from how much context the model gets (big units).
- **Build:** 45+ min for the full version (skip it); 15 min for neighbor expansion.
- **Explain:** "Small units match the query precisely and big units give the model context. I get most of the benefit by also pulling the adjacent chunks."

### 1c. Semantic or LLM chunking (SKIP)
Chroma's LLM and cluster chunkers reached 91.3-91.9 recall, against 88.1 for a 200-token recursive split. That is a small gain for more cost and more moving parts ([Chroma](https://trychroma.com/research/evaluating-chunking)).

---

## 2. Contextual chunk headers

### 2a. Deterministic breadcrumb header (DO)
- **What:** Put `"{doc_title} > {heading_path} (p.{page})\n"` in front of the text you embed and index for full-text search. Keep the raw `content` separately for display and quoting.
- **Why:** A chunk like "Revenue grew 3%" has lost its subject. The header puts it back for both the vector and the keyword retriever, at no LLM cost.
- **Build:** 5 min.
- **Explain:** "Every chunk carries a breadcrumb of where it came from, so 'grew 3%' becomes findable when someone asks about Acme's Q3 revenue."

### 2b. LLM-generated context, i.e. Anthropic Contextual Retrieval (STRETCH, behind a flag)
- **What:** For each chunk, a cheap model receives the whole document (prompt-cached) plus the chunk. It writes 50-100 tokens of situating context, which you prepend before embedding and keyword indexing. Anthropic's prompt is roughly: `<document>{{DOC}}</document> Here is the chunk we want to situate within the whole document <chunk>{{CHUNK}}</chunk> Please give a short succinct context to situate this chunk within the overall document for the purposes of improving search retrieval of the chunk. Answer only with the succinct context and nothing else.`
- **Why:** Anthropic measured top-20 retrieval failures dropping:
  - 35% with contextual embeddings alone (5.7% → 3.7%)
  - 49% when contextual BM25 is added (→ 2.9%)
  - 67% when a reranker is added on top (→ 1.9%)

  Cost was $1.02 per million document tokens using prompt caching on Claude 3 Haiku. They also note that if the whole knowledge base is under 200k tokens, you can put it all in the prompt instead ([Anthropic](https://www.anthropic.com/news/contextual-retrieval)). With today's models at $0.10/M input and $0.01/M cached input, my estimate is about half a cent per 20-page document (see §14).
- **Alternative with no LLM call:** Voyage `voyage-context-4` produces context-aware chunk embeddings at $0.12/M tokens ([Voyage pricing](https://docs.voyageai.com/docs/pricing)).
- **Build:** 30 min (Celery subtask, parallel calls, caching).
- **Explain:** "Before embedding, a cheap model writes one sentence on where the chunk sits in the document. Anthropic measured roughly half of retrieval failures removed when combined with BM25, and prompt caching makes re-reading the document for every chunk almost free."

---

## 3. Embeddings

### 3a. Model and dimensions (DO)

| Option | $/1M tokens | Dimensions | Notes |
|---|---|---|---|
| OpenAI `text-embedding-3-small` | 0.02 | 1536 by default; can be shortened with `dimensions` | Vectors are normalized to length 1; max input 8192 tokens ([guide](https://developers.openai.com/api/docs/guides/embeddings), [pricing](https://developers.openai.com/api/docs/pricing)) |
| OpenAI `text-embedding-3-large` | 0.13 | 3072 | A pgvector `vector` index caps at 2000 dims, so set `dimensions` ≤2000 or use `halfvec` |
| Voyage `voyage-4-lite` / `voyage-4` / `voyage-4-large` | 0.02 / 0.06 / 0.12, with 200M free tokens | 1024 by default; 256/512/1024/2048 available | `input_type=query\|document`; 32k context; up to 1000 texts per request ([Voyage](https://docs.voyageai.com/docs/embeddings)) |
| Local sentence-transformers | $0 | 384-1024 | Adds torch to the Docker image; slow ingest on CPU |

OpenAI's docs list nothing newer than the 3-series embeddings. My recommendation is `text-embedding-3-small` at 1536 dims if you go with OpenAI, or `voyage-4-lite` at 1024 if you go with Claude (Anthropic recommends Voyage). Record `embedding_model` and dims on every document or chunk.
- **Build:** 10 min.
- **Explain:** "Between modern API embedding models, quality differences are small next to chunking and hybrid search. I picked a cheap, well-known model, and I store the model name on each chunk because changing models means re-embedding everything."

### 3b. Batching (DO)
- **What:** Send about 100-256 chunks per request and retry 429s with exponential backoff. OpenAI's limits are reportedly ≤2048 inputs and ≤300k tokens per request ([community thread](https://community.openai.com/t/max-total-embeddings-tokens-per-request/1254699); the official docs page I fetched did not state this, so it is **partly UNVERIFIED**). Voyage allows ≤1000 texts per request with per-model token caps. For bulk backfills, the Batch API is 50% cheaper on both providers.
- **Build:** 10 min.

### 3c. Caching by content hash (DO)
- **What:** Three caches:
  1. Document dedupe: `sha256(file_bytes)` plus owner makes upload idempotent; return the existing document.
  2. Embedding cache: key `sha256(model|dims|text)` in a Postgres table or Redis.
  3. Query-embedding cache: Redis with a TTL.
- **Why:** Re-chunking or changing a prompt only pays for chunks whose text changed, and boilerplate repeated across documents is embedded once.
- **Build:** 15 min.
- **Explain:** "An embedding is a pure function of model plus text, so I key it by hash. Re-ingesting never pays twice."

---

## 4. Hybrid search: pgvector plus Postgres full-text, fused with RRF (DO)

- **What:** Run a vector top-40 and a keyword top-40 inside the same document scope, fuse them with Reciprocal Rank Fusion, `score = Σ 1/(60 + rank)`, then apply a per-document cap. This mirrors pgvector-python's official RRF example, which uses k=60 ([example](https://github.com/pgvector/pgvector-python/blob/master/examples/hybrid_search/rrf.py)). k=60 comes from Cormack et al., SIGIR 2009 ([paper](https://plg.uwaterloo.ca/~gvcormac/cormacksigir09-rrf.pdf)). The pgvector README itself recommends RRF or a cross-encoder for hybrid search ([pgvector](https://github.com/pgvector/pgvector)).
- **Why `websearch_to_tsquery`:** It "never raises syntax errors" on raw user input, and it supports quoted phrases, `or`, and `-` ([Postgres 18 docs](https://www.postgresql.org/docs/current/textsearch-controls.html)). Use a generated `tsvector` column with a GIN index, and the two-argument `to_tsvector('english', …)` ([docs](https://www.postgresql.org/docs/current/textsearch-tables.html)).

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE chunks (
  id          bigserial PRIMARY KEY,
  document_id uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  chunk_index int  NOT NULL,
  page_start  int, page_end int,
  header      text NOT NULL,                 -- "Title > Section (p.4)"
  content     text NOT NULL,                 -- raw text for display/quotes
  token_count int  NOT NULL,
  embedding   vector(1536) NOT NULL,         -- embedding of header || content
  tsv         tsvector GENERATED ALWAYS AS (to_tsvector('english', header || ' ' || content)) STORED
);
CREATE INDEX ON chunks USING hnsw (embedding vector_cosine_ops);  -- opclass must match <=>
CREATE INDEX ON chunks USING gin (tsv);
CREATE INDEX ON chunks (document_id, chunk_index);
```

```sql
BEGIN;
SET LOCAL hnsw.ef_search = 100;                 -- HNSW returns at most ef_search rows; keep >= LIMIT 40
SET LOCAL hnsw.iterative_scan = relaxed_order;  -- pgvector >= 0.8: keep scanning if the filter drops rows

WITH semantic AS (
  SELECT id, dist, ROW_NUMBER() OVER (ORDER BY dist) AS rnk
  FROM (SELECT id, embedding <=> %(qvec)s::vector AS dist      -- bind param, not a CTE column
        FROM chunks
        WHERE document_id = ANY(%(doc_ids)s::uuid[])
        ORDER BY dist LIMIT 40) s
),
lexical AS (
  SELECT id, ROW_NUMBER() OVER (ORDER BY score DESC) AS rnk
  FROM (SELECT c.id, ts_rank_cd(c.tsv, q) AS score
        FROM chunks c, websearch_to_tsquery('english', %(qtext)s) q
        WHERE c.document_id = ANY(%(doc_ids)s::uuid[]) AND c.tsv @@ q
        ORDER BY score DESC LIMIT 40) l
),
fused AS (
  SELECT COALESCE(s.id, l.id) AS chunk_id,
         COALESCE(1.0/(60 + s.rnk), 0) + COALESCE(1.0/(60 + l.rnk), 0) AS rrf,
         s.rnk AS sem_rank, l.rnk AS lex_rank, s.dist AS sem_dist
  FROM semantic s FULL OUTER JOIN lexical l ON s.id = l.id
)
SELECT * FROM (
  SELECT c.id, c.document_id, c.page_start, c.page_end, c.header, c.content,
         f.rrf, f.sem_rank, f.lex_rank, f.sem_dist,
         ROW_NUMBER() OVER (PARTITION BY c.document_id ORDER BY f.rrf DESC) AS doc_rank
  FROM fused f JOIN chunks c ON c.id = f.chunk_id
) ranked
WHERE doc_rank <= %(per_doc_cap)s        -- e.g. 4
ORDER BY rrf DESC
LIMIT %(k)s;                              -- e.g. 8
COMMIT;
```

- **Things to point out in review:**
  - Pass the query vector directly as a bind parameter. If it comes from another CTE column, the planner may not use the HNSW index for the `ORDER BY`.
  - Apply the scope filter in both branches.
  - Return `sem_rank`, `lex_rank` and `sem_dist` so you can show why each chunk was picked and compute confidence.
- **Caveat:** `ts_rank_cd` is not BM25 because it has no IDF. The upgrade path is `pg_textsearch` (BM25, v1.0) or ParadeDB `pg_search` ([pg_textsearch](https://www.postgresql.org/about/news/pg_textsearch-v10-3264)). Skip that for the 1-day build.
- **Build:** 30-40 min.
- **Explain:** "Vectors catch paraphrases; keywords catch exact names, numbers, codes and acronyms. RRF fuses by rank, so I never have to normalize two incomparable scores. A chunk ranked 10th by both retrievers scores 2/70 ≈ 0.029, which beats a chunk ranked 1st by only one of them at 1/61 ≈ 0.016, so agreement wins."

---

## 5. Reranking (SKIP by default; flag-gated STRETCH)

| Option | Cost per question | Added latency | Notes |
|---|---|---|---|
| None | $0 | 0 | Default for this build |
| Local cross-encoder `ms-marco-MiniLM-L6-v2` | $0 | SBERT lists 1,800 docs/s; CPU time for 40 pairs is roughly 0.1-0.3 s (**UNVERIFIED**) | NDCG@10 74.30; English only; pulls torch into the image ([SBERT](https://sbert.net/docs/cross_encoder/pretrained_models.html)) |
| Voyage `rerank-2.5` / `-lite` | $0.05 / $0.02 per 1M tokens; 40 × ~450 tokens ≈ 18k tokens ≈ $0.0009 / $0.0004 | One network hop, about 100-300 ms (**UNVERIFIED**) | 200M free tokens ([Voyage](https://docs.voyageai.com/docs/pricing)) |
| Cohere Rerank 4 Fast / Pro | About $2.00 / $2.50 per 1k searches (one search = one query with ≤100 docs; docs over 500 tokens are split) | Similar | The per-search price comes from a third-party tracker ([source](https://developer.puter.com/tutorials/cohere-api-pricing/)). Cohere's own page showed only dedicated-instance pricing ([Cohere](https://cohere.com/pricing)), so the per-search price is **UNVERIFIED** |
| LLM rerank (cheap model scores candidate ids) | About one extra call, ~20k input tokens ≈ $0.002 | +1-3 s (**UNVERIFIED**) | Most flexible, slowest |

- **When to skip:** chat is scoped to 1-3 documents (a few hundred candidates), hit@8 is already high in the eval, or latency is tight.
- **When to add:** "search all my docs" across thousands of documents, or the eval shows the gold chunk in the top 40 but not the top 8. That gap is exactly what a reranker closes, so measure hit@40 against hit@8. Anthropic's numbers: adding reranking took failures from 2.9% to 1.9% ([Anthropic](https://www.anthropic.com/news/contextual-retrieval)).
- **Build:** 15-20 min behind a `RERANKER=none|voyage|cohere` setting.
- **Explain:** "A bi-encoder embeds each chunk without seeing the question. A cross-encoder reads question and chunk together, so it is more accurate but too slow to run on everything, so it only reorders the top 40. I'd turn it on when the eval shows the right chunk in the top 40 but not the top 8."

---

## 6. Multi-turn conversations

### 6a. Condense the question into a standalone query (DO)
- **What:** Only when there is history, make one cheap call at temperature 0 with ~100 max tokens. Use the standard prompt: "Given a chat history and the latest user question which might reference context in the chat history, formulate a standalone question which can be understood without the chat history. Do NOT answer the question, just reformulate it if needed and otherwise return it as is." ([LangChain](https://python.langchain.com/docs/use_cases/question_answering/chat_history)). Save the result as `retrieval_query` on the assistant message.
- **Build:** 10-15 min.
- **Explain:** "Retrieval is stateless. 'What about its risks?' has nothing searchable in it, so a cheap call rewrites it to 'What are the risks of Acme's Q3 expansion?' first, and I store the rewrite so every answer can be debugged."

### 6b. Short verbatim history window instead of summary memory (DO the window, SKIP summaries)
- **What:** Send the last 3 turns verbatim, capped at about 1.5k tokens. Order the prompt as system → history → user turn containing `<sources>` and then the question, with the question last. Anthropic reports that putting the query at the end "can improve response quality by up to 30 percent in tests" ([Anthropic prompting](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices)). A rolling summary stored on the session is the answer to give for long sessions; you don't need to build it.
- **Build:** 5 min.

---

## 7. Multi-document retrieval

- **Scope by document ids (DO, 5 min):** A chat session stores `document_ids uuid[]`; empty means all of the user's documents. Always filter by owner and `status='ready'`.
- **Per-document cap and floor (DO, 10 min):** The cap is in the SQL above. When a user selects ≤4 documents explicitly, also guarantee at least one chunk from each if one clears the relevance floor (a two-pass merge in Python). In the prompt, group sources by document. **Explain:** "Without a cap, one 300-page document crowds out the rest, and a comparison question needs evidence from both sides."
- **MMR diversity (STRETCH, 15 min):** Greedily pick the chunk that maximizes `λ·sim(q,d) − (1−λ)·max sim(d, picked)`. LangChain's defaults are `fetch_k=20, lambda_mult=0.5` ([LangChain ref](https://reference.langchain.com/python/langchain-community/vectorstores/faiss/FAISS/max_marginal_relevance_search)). It is about 20 lines of numpy and removes near-duplicates from overlap or repeated boilerplate. The per-document cap matters more for comparison questions.
- **At scale (talking point only):** Use two-stage retrieval. Embed each document's summary, pick the top N documents, then search chunks inside them.

---

## 8. Citations

- **Numbered source tags (DO, 25 min including the parser):**
  ```
  <sources>
  <source id="3" document="Q3-report.pdf" pages="4-5">…escaped chunk text…</source>
  </sources>
  ```
  Instruction: "Cite every factual sentence with [n] using only ids shown above; if sources conflict, say so and cite both."
- **Mapping:** Parse with the regex `\[(\d+(?:\s*,\s*\d+)*)\]` and map n → chunk → `{document_id, title, page_start..page_end, snippet}`. Store this as `citations jsonb` on the message.
- **Post-hoc validation:**
  1. Deterministic, always on: drop any marker whose id was not in the context and log `invalid_citation_count`.
  2. STRETCH, 15 min: check each cited sentence against its chunk with lexical or embedding similarity, and flag `weak_support` below a threshold.
  3. LLM-judged faithfulness runs only in the offline eval.
- **Quote snippets:** Pick the sentence in the chunk with the highest token overlap with the cited answer sentence. Alternatively, ask the model for short verbatim quotes and check that they appear in the chunk (fuzzy match). This follows Anthropic's quote-grounding guidance ([reduce hallucinations](https://platform.claude.com/docs/en/test-and-evaluate/strengthen-guardrails/reduce-hallucinations)).
- **Streaming:** Stream text over SSE, then send a final `citations` event once validation has run.
- **Claude-native option:** Pass chunks as `search_result` blocks (`source`, `title`, `content`) with citations enabled. Responses then carry `search_result_location` block indices and `cited_text`. The pointers are guaranteed valid, and `cited_text` does not count as output tokens. However, citations combined with structured outputs returns a 400, and this ties you to one provider ([search results](https://platform.claude.com/docs/en/build-with-claude/search-results), [citations](https://platform.claude.com/docs/en/build-with-claude/citations)). Use numbered markers if you want a provider-neutral design.
- **Explain:** "The model only ever sees numbered sources and cites by number. My code maps numbers to document, page and snippet, and discards any number that wasn't in the context, so a citation can never point at something that wasn't retrieved."

---

## 9. Grounding, refusal and confidence

- **Grounding prompt (DO, part of §8):**
  - Answer only from the sources; no outside knowledge.
  - If the answer isn't there, start the reply with the fixed phrase "I couldn't find this in your documents." and mention what related material was found.
  - Point out conflicts between sources.

  Giving explicit permission to say "I don't know" and restricting the model to the provided information are both Anthropic recommendations ([Anthropic](https://platform.claude.com/docs/en/test-and-evaluate/strengthen-guardrails/reduce-hallucinations)). Because the refusal phrase is fixed, code can detect it reliably.
- **Retrieval gate (DO, 10 min):** If there is no keyword hit and the best semantic similarity (`1 − sem_dist`) is below a floor, return the refusal with `grounded=false` and skip the LLM call. Calibrate the floor on the unanswerable questions in your golden set; similarity scales differ between embedding models, so don't copy a number from a blog.
- **Confidence (DO, 10 min):** Return `{grounded, confidence: high|medium|low, signals: {top_similarity, lexical_match, valid_citations, refused}}`, computed by rules. Don't ask the model to rate itself.
- **Explain:** "Confidence comes from things I can measure: how close the best evidence was, whether keywords matched, and how many valid citations survived. Questions that are clearly out of scope never reach the LLM."

---

## 10. Prompt injection through document content (DO, ~15 min)

The threat is indirect injection: an uploaded document contains "ignore previous instructions…" ([OWASP LLM01:2025](https://genai.owasp.org/llmrisk/llm01-prompt-injection/)). Defenses, in layers:
1. **Least privilege (the strongest layer):** The chat model has no tools, takes no actions, and has no secrets in its prompt. Documents are scoped to their owner, so a user can only poison their own results.
2. **Instruction hierarchy:** Rules live in the system prompt. Document text sits in the user turn inside `<sources>`, labeled as untrusted data that must never be followed. OpenAI's Model Spec says quoted text, files and tool outputs "have no authority", and any instructions in them must be treated as information ([Model Spec](https://model-spec.openai.com/2026-08-18.html)).
3. **Delimiters a document can't forge:** Escape `<` and `>` in chunk text, or use a random nonce in the tag name. This is Microsoft's "spotlighting" delimiting. In their study, datamarking and encoding lowered attack success rates further ([Spotlighting](https://arxiv.org/pdf/2403.14720)).
4. **Inert output:** Render escaped markdown and never auto-load remote images or links (markdown images are an exfiltration channel). Validate structured-output enums in code.
5. **Flag at ingestion:** A regex or LLM check sets `injection_suspected` in the document insights. It is a heuristic for the UI, not a defense.
6. **Test it:** Put one poisoned document in the golden set and show it in the demo.
- **Explain:** "I can't make the model immune, so I make injection low-impact. Document text is fenced and labeled as data, the chat model has no tools to abuse, the output is rendered inertly, and a poisoned document in my eval set proves it."

---

## 11. Summarization, structured insights, follow-ups and customization

### 11a. Stuff, map-reduce or refine

| Strategy | When | Trade-off |
|---|---|---|
| **Stuff** (whole document in one call) | Documents ≤ ~80-100k tokens, which covers nearly every demo document | One call. Cheap models now have huge windows: GPT-6 Luna has 1.05M context ([OpenAI](https://developers.openai.com/api/docs/models/gpt-6-luna)) |
| **Map-reduce** | Above that threshold | Summarize ~20-30k-token sections in parallel (Celery group or `asyncio.gather`), then reduce into the schema. Avoids price cliffs: Claude Haiku 5.5 charges 5x for prompts over 100k ([Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing)); GPT-6 Luna charges 2x input and 1.5x output above 272k. Also avoids long-context degradation: all 18 models Chroma tested got worse as input length grew ([context rot](https://trychroma.com/research/context-rot)), and the "lost in the middle" U-curve ([Liu et al.](https://arxiv.org/abs/2307.03172)) |
| **Refine** (sequential) | SKIP | Latency grows linearly with document length and can't be parallelized |

- **Build:** stuff 15 min; map-reduce fallback +20 min (STRETCH).

### 11b. Structured extraction in one schema-constrained call (DO, 20 min)
- **What:** One call returns a Pydantic `DocumentInsights{summary, key_points[{point, page}], document_type, category(enum), tags[≤8], entities[{name,type}], sentiment(enum), sentiment_rationale, suggested_questions[5], language, injection_suspected}`.
  - OpenAI: Structured Outputs with `strict: true`. All fields must be required and `additionalProperties: false`; handle the `refusal` field ([OpenAI](https://developers.openai.com/api/docs/guides/structured-outputs)).
  - Claude: `output_config.format` / `messages.parse()`.
  - Put the document first and the instructions last, require a page for each key point, and allow only document-based content.
  - Validate, retry once, and on failure set `insights_status=failed` while the document stays chat-ready.
  - Store `model`, `prompt_version`, tokens and cost.
- **Explain:** "One schema-constrained call produces every insight the API exposes, with no fragile parsing. Chunking and embedding are a separate step, so an insights failure never blocks chat."

### 11c. Summary customization (DO, 15 min)
`POST /documents/{id}/summaries {length: short|medium|detailed, focus?: str, tone: neutral|executive|technical|simple}`. Map length to word budgets. For focus, instruct: "prioritize X; if the document doesn't cover X, say so". Cache results in `document_summaries`, keyed on (document_id, content_hash, params, prompt_version, model).

### 11d. Follow-up question suggestions (DO, 10 min)
- Cold start: show the 5 `suggested_questions` from the insights.
- Each turn: a cheap call after the answer, given the standalone question, the answer, and the headers of chunks that were retrieved but not cited. It returns 3 questions of ≤12 words each and is sent as the final SSE event.
- **Explain:** "Suggestions come from evidence we retrieved but didn't use, so every suggested question is one the documents can actually answer."

---

## 12. Evaluation you can run in 30 minutes (DO, 30-40 min)

- **Golden set:** 20 JSONL rows of `{question, history?, doc_ids, expected_doc, expected_substring, reference_answer, answerable}`.
  - 12 single-document factoid or numeric questions, including exact codes and names to exercise keyword search
  - 3 multi-document comparison questions
  - 3 unanswerable questions
  - 1 two-turn follow-up
  - 1 injection probe

  Synthetic documents with planted facts make labeling trivial. Label gold evidence by **expected substring, not chunk id**, so the labels survive chunk-size experiments.
- **Retrieval metrics** (deterministic, no LLM, seconds to run): hit@k for k = 1, 5, 8, 40 (share of questions with a gold chunk in the top k), and MRR (mean of 1/rank of the first gold chunk, 0 if missing). Run vector-only, keyword-only and hybrid, plus contextual and rerank variants if built, and produce a comparison table for the README.
- **Generation metrics:**
  - **Faithfulness:** claim-based LLM judge. Extract claims, check each against the retrieved context, and compute supported/total in code. Use temperature 0 and a judge stronger than the generator. Calibrate on 20-50 human labels when time allows ([Langfuse](https://langfuse.com/resources/engineering/rag-faithfulness-evaluation)).
  - **Correctness** against the reference answer, also by judge.
  - **Refusal accuracy** on the unanswerable questions, deterministic via the fixed refusal phrase.
  - **Citation validity:** % of markers that are valid and % of answers that cite a gold chunk, deterministic.
- **RAGAS:** Use its vocabulary (faithfulness, response relevancy, context precision, context recall, noise sensitivity; [Ragas](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/)), but write your own ~40-line judge. It is easier to explain than a dependency.
- **Cost:** under $0.50 per full run (my estimate).
- **Explain:** "I measure retrieval separately from generation. Hit@k and MRR say whether the right chunk was found; faithfulness and refusal accuracy say whether the model stayed inside it. The hybrid-versus-vector table is the evidence for my design."

---

## 13. pgvector: HNSW settings, exact scans, scaling and filter pitfalls

- **Version:** pgvector 0.8.7. Docker images `pgvector/pgvector:0.8.7-pg17` or `pg18` ([pgvector](https://github.com/pgvector/pgvector)).
- **Parameters:**
  - `m` = 16 by default: max connections per layer, which drives memory and recall.
  - `ef_construction` = 64 by default: candidate-list size while building, which trades build time for recall.
  - `hnsw.ef_search` = 40 by default, set per query with `SET LOCAL`. Results are capped at `ef_search`, so keep it at or above your LIMIT.
  - The index opclass must match the operator (`vector_cosine_ops` ↔ `<=>`). OpenAI vectors are normalized, so inner product ranks identically and is slightly faster.
  - Index dimension limits: `vector` ≤2000, `halfvec` ≤4000, binary ≤64,000.
- **When an exact scan is fine:** Per-document chat filters down to hundreds or thousands of rows, so a btree on `document_id` plus an exact sort gives perfect recall. The README says "Exact indexes work well for conditions that match a low percentage of rows." Confirm the plan with `EXPLAIN ANALYZE`. For a whole corpus of ≲10^5 chunks, exact scan times are probably tens to hundreds of ms (**UNVERIFIED**; measure it).
- **Filter pitfall:** With approximate indexes, the filter is applied after the index scan. In the README's words: "If a condition matches 10% of rows, with HNSW and the default `hnsw.ef_search` of 40, only 4 rows will match on average." Fixes:
  - Iterative scans (0.8.0+): `hnsw.iterative_scan = strict_order | relaxed_order`, bounded by `hnsw.max_scan_tuples` (default 20,000) and `hnsw.scan_mem_multiplier`. With relaxed order, re-sort in a materialized CTE; my SQL does this with `ROW_NUMBER() OVER (ORDER BY dist)`.
  - Partial indexes when there are few distinct filter values.
  - List partitioning when there are many, including per-tenant isolation.
- **Building indexes:** Create the index after bulk load, give it enough `maintenance_work_mem` for the graph to fit, and raise `max_parallel_maintenance_workers`.
- **Scaling path:**
  - **Thousands of documents** (1,000 docs × ~50 chunks ≈ 50k rows): defaults are fine.
  - **1M-10M chunks:** 1M × 1536 dims × 4 bytes ≈ 6.1 GB of raw vectors before the graph. `halfvec` halves that; `dimensions=512-768` cuts it further. Add tenant partitions and iterative scans, tune `ef_search` between 40 and 200, and monitor recall against exact search (`SET LOCAL enable_indexscan = off`).
  - **100M+:** binary quantization with re-ranking on the full vectors (the README pattern), or pgvectorscale or a dedicated vector database (**UNVERIFIED** specifics).
- **Explain:** "HNSW is a layered nearest-neighbor graph. `m` is neighbors per node, `ef_construction` is build effort, `ef_search` is per-query effort. Per-document chat is selective enough for an exact btree-filtered scan with perfect recall. HNSW plus iterative scans is for 'search everything'."

---

## 14. Cost and latency with current cheap models

**Prices per 1M tokens, standard tier, checked today** ([OpenAI](https://developers.openai.com/api/docs/pricing), [Anthropic](https://platform.claude.com/docs/en/about-claude/pricing)). The Batch API is 50% off on both providers.

| Model | Input | Cached input | Output | Notes |
|---|---|---|---|---|
| `gpt-6-luna` | 0.10 | 0.01 | 0.50 | 1.05M context; 2x input / 1.5x output above 272k input; reasoning effort defaults to medium, so set `none` or `low` for chat |
| `gpt-5-mini` | 0.25 | 0.025 | 2.00 | Reasoning tokens are billed as output |
| `gpt-4.1-mini` | 0.40 | 0.10 | 1.60 | Mature, non-reasoning |
| `gpt-4o-mini` | 0.15 | 0.075 | 0.60 | |
| Claude Haiku 5.5 (prompt ≤100k) | 0.10 | 0.01 | 0.50 | Released 2026-10-07, so very new for a demo. 0.50/2.50 above 100k. Thinking is on by default; lower effort or disable it for chat |
| Claude Haiku 4.5 | 1.00 | 0.10 | 5.00 | |
| Claude Sonnet 5.5 | 2.00 | 0.10 | 10.00 | |
| `text-embedding-3-small` / `voyage-4-lite` | 0.02 | n/a | n/a | |

**Per document** (20 pages ≈ 10k tokens ≈ 25 chunks, on a $0.10/$0.50 model):

| Step | Cost |
|---|---|
| Embeddings | ≈ $0.0002 |
| Insights call (stuff: ~11k in, ~1k out) | ≈ $0.0016 |
| Optional LLM contextual headers | ≈ $0.005 |
| **Total** | **≈ $0.002 per doc, or ≈ $0.007 with contextual headers. 1,000 docs ≈ $2-7** |

**Per question** (~6k-token prompt, ~400 output tokens):

| Step | Cost |
|---|---|
| Condense | ≈ $0.0001 |
| Answer | ≈ $0.0008 |
| Follow-ups | ≈ $0.0002 |
| **Total** | **≈ $0.001, about 1,000 questions per $1** |
| Adding Voyage rerank | ≈ +$0.0009 |
| Using Sonnet 5.5 for the answer instead | ≈ $0.017 per question |

**Why not just put the whole document in the prompt?** A 90k-token document costs ≈ $0.009 per question uncached, but only ≈ $0.001 cached on Haiku 5.5. So cost alone doesn't rule it out. What does: latency, context rot, page-precise citations, and searching across thousands of documents. That is a good, honest answer if the panel asks.

**Latency** (**UNVERIFIED**; measure it and log each stage):

| Stage | Rough figure |
|---|---|
| Query embedding | 100-300 ms |
| Hybrid SQL at ≤10^5 chunks | <50 ms |
| Condense call | 0.4-1 s |
| Answer time-to-first-token, reasoning off or low | 0.5-1.5 s |

Skip the condense call when there is no history, and generate follow-ups after the answer has streamed. Log per message: model, input/output/cached tokens, `cost_usd`, and timings `{condense, embed, retrieve, ttft, total}`. That data feeds the Processing Metrics API.

---

## 15. Ranked list for a 1-day build

**DO, in this order** (core chat is about 2h45m; with the eval about 3h20m):
1. Page-aware recursive chunking with page and heading metadata (20)
2. Deterministic breadcrumb header on each chunk (5)
3. Batched embeddings, hash cache and idempotent upload (20)
4. Hybrid SQL with RRF k=60, doc scoping, `ef_search`, iterative scan (35)
5. Grounded answer prompt: fenced numbered sources, question last, fixed refusal phrase (15)
6. Citation parsing, id validation, snippets, final SSE `citations` event (25)
7. Query condensation plus a 3-turn history window (15)
8. Retrieval gate plus rule-based confidence (10)
9. Per-document cap in SQL (10)
10. Injection hardening plus a poisoned-document test (10)
11. 20-question golden set with hit@k/MRR, faithfulness, refusal and citation-validity metrics, and the comparison table in the README (35)
12. Ingestion-side AI, about 45 min more, which overlaps the "AI insights" requirement: one structured insights call (20), follow-up suggestions (10), summary customization with caching (15)

**STRETCH** (each behind a flag, and only kept if the eval shows a gain):
- LLM contextual retrieval (30)
- Voyage or Cohere reranker (20)
- Map-reduce summarization fallback (20)
- MMR (15)
- Neighbor-chunk expansion (15)
- Citation support check (15)

**SKIP for one day:**
- LangChain or LlamaIndex end-to-end chains: they hide the retrieval, prompt and citation logic you have to defend. Use only the text splitter.
- Semantic or LLM chunking, and full parent-child or auto-merging hierarchies: small gains for more complexity.
- HyDE, multi-query expansion, agentic or iterative retrieval, GraphRAG.
- Summary memory for chats, and refine-style summarization.
- Local cross-encoder (torch image bloat) and LLM reranking (latency).
- BM25 extensions (`pg_textsearch`, ParadeDB), binary quantization, partitioning, pgvectorscale: scaling talking points only.
- Ragas as a dependency (your own judge is easier to explain), fine-tuned embeddings, and answer caching (multi-turn staleness).

**Still UNVERIFIED:**
- OpenAI's per-request embedding limits (2048 inputs / 300k tokens; community source only)
- Cohere's per-search API price
- Every latency figure
- CPU timings for cross-encoders
- Exact-scan speed at 10^5 rows
- pgvectorscale and 100M+ scaling specifics