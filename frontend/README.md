# Vault AI dashboard

React 19 + TypeScript + Vite, styled with Tailwind v4. It is the demo UI for the Vault AI API: upload documents, read
their AI insights, chat with them with page-level citations, and watch cost and latency metrics.

```bash
pnpm install
pnpm dev        # http://localhost:5173; /api, /health, /docs are proxied to the API on :8000 (VAULT_API= overrides)
pnpm build      # type-check (tsc -b) + production bundle in dist/
pnpm lint       # oxlint
pnpm test       # node --test: SSE parser, citation streaming, answer helpers, markdown safety, readiness timeout
```

In production the Dockerfile builds `dist/` and FastAPI serves it from `app/static/spa`, so the browser talks to one
origin: no CORS, and no API URL baked in at build time. Every request uses a relative URL.

## How it talks to the API

| Concern | Where | Notes |
|---|---|---|
| HTTP client | `src/api/client.ts` | Sends `X-API-Key` from localStorage (default `vault-demo-key`, changeable in Settings). A non-2xx response becomes an `ApiError` built from the envelope `{error: {code, message, details, request_id}}`, and toasts show the code, message and request id. |
| SSE | `src/api/sse.ts`, `src/api/sseParser.ts` | `fetch` + `ReadableStream`, not `EventSource`, because `EventSource` cannot send the API key header or POST a body. The parser buffers events split across reads, ignores `: ping` comments and unknown event names, and the caller cancels with an `AbortController`. |
| Types | `src/api/types.ts` | Hand-written mirrors of `app/schemas/*.py`. Pydantic `Decimal` arrives as a string (`Money`). |
| Endpoint calls | `src/api/{documents,chat,metrics}.ts` | One typed function per endpoint. |

## Pages and endpoints

### Documents (`/`)
| UI | Endpoint |
|---|---|
| Drag-and-drop or picked files, one request per file. 202 means queued. 200 with `duplicate:true` shows the toast "Already uploaded - no new cost". 413/415/422 errors appear inline next to the file. | `POST /api/v1/documents` (multipart `file`) |
| Table with filters (status, category, tag, search) and cursor "Load more" | `GET /api/v1/documents?status=&category=&tag=&q=&limit=&cursor=` |
| Live status chips (stage and progress) for in-flight rows, at most 3 streams at a time | `GET /api/v1/documents/{id}/events` (SSE `status`, `deleted`) |
| Row refresh when processing finishes | `GET /api/v1/documents/{id}` |
| Reprocess / delete (with confirmation) | `POST /api/v1/documents/{id}/reprocess` · `DELETE /api/v1/documents/{id}` |

### Document detail (`/documents/:id`)
| UI | Endpoint |
|---|---|
| Metadata, status, live progress | `GET /api/v1/documents/{id}` · `.../events` |
| Insights: summary, key points with page links, type, category, tags, entities, sentiment and rationale, risk flags (severity, verified quote, page link), "Sampled coverage" badge, pending/processing/failed states. Page links read "p. N" for PDFs; DOCX/Markdown/TXT have sections or parts instead of pages, so their links show the stored label ("§ Termination") | `GET /api/v1/documents/{id}/insights` · `.../pages/{n}` (labels, non-PDF only) |
| Suggested question "Ask": creates a chat scoped to this document, opens it, and sends the question | `POST /api/v1/chat/sessions` then the chat stream |
| Custom summary (length, tone, focus) with a "Cached" badge | `POST /api/v1/documents/{id}/summaries` |
| Page viewer with prev/next; page links highlight the quoted text | `GET /api/v1/documents/{id}/pages/{n}` |
| Original file (fetched as a blob because the route needs the API key header) | `GET /api/v1/documents/{id}/file` |
| Reprocess all / regenerate insights only | `POST /api/v1/documents/{id}/reprocess` `{scope}` |

### Chat (`/chat`, `/chat/:sessionId`)
| UI | Endpoint |
|---|---|
| Sessions sidebar: list, new, delete | `GET`/`POST /api/v1/chat/sessions` · `DELETE /api/v1/chat/sessions/{id}` |
| New-chat dialog with scope picker (all documents, or up to 20 ready ones) | `GET /api/v1/documents?status=ready` · `POST /api/v1/chat/sessions` |
| Header, scope badges, starter-question chips | `GET /api/v1/chat/sessions/{id}` (`starter_questions`, `warnings`) |
| History, "Load earlier messages" | `GET /api/v1/chat/sessions/{id}/messages?cursor=` |
| Streaming answer: tokens, `[S#]` citation chips (a partial `[S1` at the end of a delta is held back until complete), "Interpreted as" line, confidence badge, "Unverified" banner (ungrounded or invalid citations), not-found styling, follow-up chips, warning notes, Stop and Retry (same `client_message_id`) | `POST /api/v1/chat/sessions/{id}/messages/stream` (SSE `meta`, `token`, `citations`, `followups`, `done`, `error`) |
| Non-streaming toggle | `POST /api/v1/chat/sessions/{id}/messages` (JSON) |
| Citation popover, then "Open page" in a modal page viewer | `GET /api/v1/documents/{id}` · `.../pages/{n}` |
| "Why this answer?" drawer: sources from `meta` while streaming; once done, retrieval candidates (RRF, vector rank, keyword rank, similarity = 1 - `sem_dist`, id hit), grounding checks, per-step timings, model, tokens, cost | stored message `retrieval`/`signals` + `done.timings` |

### Metrics (`/metrics`), refreshed every 10 s while the tab is visible
| Card | Endpoint |
|---|---|
| KPI tiles, Documents | `GET /api/v1/metrics/documents` |
| Processing (success rate, stage p50/p95, failures by code, queue depth, throughput, recent failures) | `GET /api/v1/metrics/processing?window=24h` |
| AI usage and cost, grouped by operation / model / day, spend against the daily limit | `GET /api/v1/metrics/ai-usage?window=7d&group_by=` |
| Chat quality and latency | `GET /api/v1/metrics/chat?window=7d` |
| API routes | `GET /api/v1/metrics/system?window=1h` |

A route that is not deployed (404 `NOT_FOUND`, or the SPA's HTML fallback) renders as "Not available on this server
yet" instead of an error.

### Header
"Vault AI", navigation, a health dot polled every 15 s from `GET /health/ready`: ok (green), degraded (amber, with
the failing checks in the tooltip), unavailable (red, including "API unreachable" and no answer within 5 s). Settings stores the API key and
reloads the page so no other owner's data stays on screen.

## Code layout

```
src/api/         HTTP client, SSE reader/parser, typed endpoint calls, TS mirrors of app/schemas
src/hooks/       useApi (fetch + abort + reload), useDocumentEvents (SSE fan-out), useChat (stream state), useToast
src/lib/         pure helpers: citation markers, answer view model, formatting
src/components/  UI primitives, dialogs, toasts, citation chip, page viewer, charts; one folder per page
src/pages/       Documents, DocumentDetail, Chat, Metrics
tests/           node:test unit tests for the pure helpers
```

Answers and summaries are rendered with `react-markdown` with `skipHtml`: raw HTML in model output is dropped, and
nothing uses `dangerouslySetInnerHTML`.
