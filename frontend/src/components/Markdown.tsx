import { useMemo, type ComponentProps } from 'react'
import ReactMarkdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { CITE_HREF_PREFIX, linkifyCitations, type CitationInfo } from '../lib/citations'
import { CitationChip } from './CitationChip'

interface MarkdownProps {
  text: string
  /** When given, [S#] markers render as citation chips resolved through this lookup. */
  resolveCitation?: (ref: string) => CitationInfo
  onOpenPage?: (info: CitationInfo) => void
}

/**
 * Model output rendered as markdown, safely: react-markdown builds React elements (never innerHTML) and `skipHtml`
 * drops any raw HTML in the text, so a document that smuggles <script> or <img onerror> into an answer renders
 * nothing. GFM is enabled for the comparison tables answers sometimes contain.
 */
export function Markdown({ text, resolveCitation, onOpenPage }: MarkdownProps) {
  const components = useMemo<Components>(
    () => ({
      a: ({ href, children }: ComponentProps<'a'>) => {
        if (resolveCitation && href?.startsWith(CITE_HREF_PREFIX)) {
          return <CitationChip info={resolveCitation(href.slice(CITE_HREF_PREFIX.length))} onOpenPage={onOpenPage} />
        }
        return (
          <a href={href} target="_blank" rel="noopener noreferrer">
            {children}
          </a>
        )
      },
    }),
    [resolveCitation, onOpenPage],
  )
  const source = resolveCitation ? linkifyCitations(text) : text
  return (
    <div className="prose-answer">
      <ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml components={components}>
        {source}
      </ReactMarkdown>
    </div>
  )
}
