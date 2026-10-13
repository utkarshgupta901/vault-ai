import { AlertTriangle, CircleHelp, Lightbulb, MessageSquarePlus, ShieldAlert, Sparkles } from 'lucide-react'
import type { ReactNode } from 'react'
import { isAbortError, isRouteMissing } from '../../api/client'
import { getInsights, getPage } from '../../api/documents'
import type { DocumentInsights, Entity, InsightsStatus, RiskFlag } from '../../api/types'
import { useApi } from '../../hooks/useApi'
import { hasRealPages, pageRefLabel } from '../../lib/documents'
import { fmtDate, fmtDateTime, humanize } from '../../lib/format'
import { Badge, Button, Card, CardHeader, EmptyState, ErrorState, Skeleton, Spinner, type Tone } from '../ui'

interface InsightsPanelProps {
  documentId: string
  /** Decides how page chips are labelled: PDFs have pages, other formats have sections/parts (lib/documents.ts). */
  mimeType: string
  /** Live insights status from the document row; a change triggers a refetch of the insights. */
  status: InsightsStatus
  onOpenPage: (page: number, highlight?: string) => void
  onAsk: (question: string) => void
  onRegenerate: () => void
  askBusy: boolean
}

/** Structured AI insights (GET /documents/{id}/insights). Quotes and pages were verified server-side. */
export function InsightsPanel({ documentId, mimeType, status, onOpenPage, onAsk, onRegenerate, askBusy }: InsightsPanelProps) {
  const { data, error, reload } = useApi(`insights:${documentId}:${status}`, (signal) => getInsights(documentId, signal))

  let body
  if (error && !data) {
    body = isRouteMissing(error) ? (
      <EmptyState icon={Sparkles} title="Insights are not available on this server yet" />
    ) : (
      <ErrorState error={error} onRetry={reload} what="insights" />
    )
  } else if (!data) {
    body = <InsightsSkeleton />
  } else if (data.status === 'pending' || data.status === 'processing') {
    body = (
      <div className="p-4">
        <p className="mb-4 flex items-center gap-2 text-sm text-zinc-600">
          <Spinner /> {data.status === 'pending' ? 'Insights are queued; they start once the document is ready.' : 'Generating insights…'}
        </p>
        <InsightsSkeleton />
      </div>
    )
  } else if (data.status === 'failed') {
    body = (
      <EmptyState icon={AlertTriangle} title="Insights could not be generated" action={<Button onClick={onRegenerate}>Regenerate insights</Button>}>
        {data.error_code && <span className="font-mono text-xs text-red-600">{data.error_code}</span>}
        <p>Chat still works: insights are a separate step after the document is indexed.</p>
      </EmptyState>
    )
  } else if (!data.insights) {
    // "ready" without a payload: e.g. processed before the insights step existed. Regenerating fills it in.
    body = (
      <EmptyState icon={Sparkles} title="No insights stored for this document" action={<Button onClick={onRegenerate}>Generate insights</Button>}>
        <p>Chat still works: insights are a separate step after the document is indexed.</p>
      </EmptyState>
    )
  } else {
    body = (
      <InsightsBody documentId={documentId} mimeType={mimeType} insights={data.insights} onOpenPage={onOpenPage} onAsk={onAsk} askBusy={askBusy} />
    )
  }

  return (
    <Card>
      <CardHeader
        title={
          <span className="flex items-center gap-1.5">
            <Sparkles className="size-4 text-accent-600" aria-hidden /> AI insights
          </span>
        }
        description={data?.updated_at ? `Updated ${fmtDateTime(data.updated_at)}` : 'Generated once per document, after indexing'}
        actions={data?.insights?.coverage === 'sampled' && <Badge tone="amber" title="The document was too long to read in full; insights are based on a sample of its pages.">Sampled coverage</Badge>}
      />
      {body}
    </Card>
  )
}

function InsightsSkeleton() {
  return (
    <div className="space-y-3 p-4">
      <Skeleton className="h-5 w-2/3" />
      <Skeleton className="h-4 w-full" />
      <Skeleton className="h-4 w-11/12" />
      <Skeleton className="h-4 w-4/5" />
    </div>
  )
}

const SEVERITY_TONE: Record<RiskFlag['severity'], Tone> = { high: 'red', medium: 'amber', low: 'neutral' }
const SENTIMENT_TONE: Record<DocumentInsights['sentiment'], Tone> = { positive: 'green', neutral: 'neutral', negative: 'red', mixed: 'amber' }

const NO_LABELS: ReadonlyMap<number, string> = new Map()

/**
 * Stored labels ("§ Termination", "part 2") of the pages the insights point at, fetched only for documents without
 * real pages (see hasRealPages). One small GET per distinct page, one at a time, so a dozen chips never send a burst;
 * a page that fails to load keeps its fallback label and its chip still opens the viewer.
 */
function usePageLabels(documentId: string, pages: number[], enabled: boolean): ReadonlyMap<number, string> {
  const key = enabled && pages.length > 0 ? `page-labels:${documentId}:${pages.join(',')}` : null
  const { data } = useApi(key, async (signal) => {
    const labels = new Map<number, string>()
    for (const page of pages) {
      try {
        labels.set(page, (await getPage(documentId, page, signal)).label)
      } catch (err) {
        if (isAbortError(err)) throw err
      }
    }
    return labels
  })
  return data ?? NO_LABELS
}

function PageLink({ page, label, onOpen }: { page: number | null; label: string; onOpen: () => void }) {
  if (page == null) return null
  return (
    <button
      type="button"
      onClick={onOpen}
      title={label}
      className="ml-1 max-w-56 truncate rounded bg-zinc-100 px-1.5 py-0.5 align-bottom font-mono text-[0.7rem] text-zinc-600 hover:bg-accent-50 hover:text-accent-700"
      aria-label={`Open ${label} in the page viewer`}
    >
      {label}
    </button>
  )
}

interface InsightsBodyProps {
  documentId: string
  mimeType: string
  insights: DocumentInsights
  onOpenPage: (page: number, highlight?: string) => void
  onAsk: (q: string) => void
  askBusy: boolean
}

function InsightsBody({ documentId, mimeType, insights, onOpenPage, onAsk, askBusy }: InsightsBodyProps) {
  const pages = [...new Set([...insights.key_points, ...insights.risk_flags].flatMap((item) => (item.page == null ? [] : [item.page])))].sort((a, b) => a - b)
  const labels = usePageLabels(documentId, pages, !hasRealPages(mimeType))
  const pageLabel = (page: number | null) => (page == null ? '' : pageRefLabel(mimeType, page, labels.get(page)))
  return (
    <div className="space-y-6 p-4">
      <div>
        <div className="flex flex-wrap items-center gap-1.5">
          <Badge tone="violet">{humanize(insights.category)}</Badge>
          <Badge>{insights.document_type}</Badge>
          <Badge tone={SENTIMENT_TONE[insights.sentiment]} title={insights.sentiment_rationale}>
            Sentiment: {insights.sentiment}
          </Badge>
          {insights.document_date && <Badge>Dated {fmtDate(insights.document_date)}</Badge>}
          {insights.injection_suspected && (
            <Badge tone="red" icon={ShieldAlert} title="The document contains text addressed to AI systems. It is treated as data and its instructions are ignored.">
              Prompt-injection text detected
            </Badge>
          )}
        </div>
        <h3 className="mt-3 text-base font-semibold text-zinc-900">{insights.title}</h3>
        <p className="mt-1.5 text-sm leading-relaxed text-zinc-700">{insights.summary}</p>
        {insights.sentiment_rationale && <p className="mt-2 text-xs text-zinc-500">Sentiment: {insights.sentiment_rationale}</p>}
      </div>

      {insights.key_points.length > 0 && (
        <Section title="Key points" icon={Lightbulb}>
          <ul className="space-y-1.5 text-sm text-zinc-700">
            {insights.key_points.map((kp, i) => (
              <li key={i} className="flex gap-2">
                <span className="mt-2 size-1 shrink-0 rounded-full bg-zinc-400" aria-hidden />
                <span>
                  {kp.point}
                  <PageLink page={kp.page} label={pageLabel(kp.page)} onOpen={() => kp.page != null && onOpenPage(kp.page)} />
                </span>
              </li>
            ))}
          </ul>
        </Section>
      )}

      {insights.risk_flags.length > 0 && (
        <Section title="Risk flags" icon={AlertTriangle} note="Severity is model-assigned; each quote was verified against the document text.">
          <ul className="space-y-2.5">
            {insights.risk_flags.map((flag, i) => (
              <li key={i} className="rounded-md border border-zinc-200 p-3">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge tone={SEVERITY_TONE[flag.severity]}>{humanize(flag.severity)}</Badge>
                  <span className="text-sm font-medium text-zinc-900">{flag.type}</span>
                  <PageLink page={flag.page} label={pageLabel(flag.page)} onOpen={() => flag.page != null && onOpenPage(flag.page, flag.quote)} />
                </div>
                <blockquote className="mt-2 border-l-2 border-zinc-200 pl-3 text-sm text-zinc-600 italic">“{flag.quote}”</blockquote>
              </li>
            ))}
          </ul>
        </Section>
      )}

      {insights.entities.length > 0 && (
        <Section title="Entities">
          <EntityGroups entities={insights.entities} />
        </Section>
      )}

      {insights.tags.length > 0 && (
        <Section title="Tags">
          <div className="flex flex-wrap gap-1.5">
            {insights.tags.map((t) => (
              <Badge key={t}>{t}</Badge>
            ))}
          </div>
        </Section>
      )}

      {insights.suggested_questions.length > 0 && (
        <Section title="Suggested questions" icon={CircleHelp} note="Ask starts a chat scoped to this document.">
          <ul className="space-y-1.5">
            {insights.suggested_questions.map((q) => (
              <li key={q} className="flex items-center justify-between gap-3 rounded-md bg-zinc-50 px-3 py-2 text-sm text-zinc-700">
                <span>{q}</span>
                <Button size="sm" icon={MessageSquarePlus} onClick={() => onAsk(q)} disabled={askBusy}>
                  Ask
                </Button>
              </li>
            ))}
          </ul>
        </Section>
      )}

      {(insights.model || insights.prompt_version) && (
        <p className="text-xs text-zinc-400">
          {insights.model} {insights.prompt_version && `· prompt ${insights.prompt_version}`} {insights.language && `· language ${insights.language}`}
        </p>
      )}
    </div>
  )
}

function Section({ title, icon: Icon, note, children }: { title: string; icon?: typeof Lightbulb; note?: string; children: ReactNode }) {
  return (
    <section>
      <h4 className="mb-2 flex items-center gap-1.5 text-xs font-semibold tracking-wide text-zinc-500 uppercase">
        {Icon && <Icon className="size-3.5" aria-hidden />}
        {title}
      </h4>
      {note && <p className="-mt-1 mb-2 text-xs text-zinc-400">{note}</p>}
      {children}
    </section>
  )
}

function EntityGroups({ entities }: { entities: Entity[] }) {
  const groups = new Map<string, string[]>()
  for (const e of entities) groups.set(e.type, [...(groups.get(e.type) ?? []), e.name])
  return (
    <dl className="grid gap-2 sm:grid-cols-2">
      {[...groups].map(([type, names]) => (
        <div key={type}>
          <dt className="text-xs text-zinc-500">{humanize(type)}</dt>
          <dd className="mt-0.5 flex flex-wrap gap-1">
            {[...new Set(names)].map((n) => (
              <Badge key={n} tone="blue">
                {n}
              </Badge>
            ))}
          </dd>
        </div>
      ))}
    </dl>
  )
}
