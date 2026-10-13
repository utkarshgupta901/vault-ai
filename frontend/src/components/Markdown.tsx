import { useMemo, type ComponentProps } from 'react'
import ReactMarkdown, { type Components } from 'react-markdown'
import { CITE_HREF_PREFIX, linkifyCitations, type CitationInfo } from '../lib/citations'
import { MARKDOWN_OPTIONS } from '../lib/markdown'
import { CitationChip } from './CitationChip'

interface MarkdownProps {
  text: string
  /** When given, [S#] markers render as citation chips resolved through this lookup. */
  resolveCitation?: (ref: string) => CitationInfo
  onOpenPage?: (info: CitationInfo) => void
}

/**
 * Model output rendered as markdown, safely: react-markdown builds React elements (never innerHTML), raw HTML is
 * dropped, and markdown images are never rendered, so text a poisoned document makes the model write can neither run
 * script nor make the browser request an attacker's URL while the answer streams. See lib/markdown.ts for the options
 * and why each one is there.
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
      <ReactMarkdown {...MARKDOWN_OPTIONS} components={components}>
        {source}
      </ReactMarkdown>
    </div>
  )
}
