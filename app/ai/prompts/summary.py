"""summary.v1: a custom summary of one document (sync API call, cached in document_summaries).

The variable options (length, tone, focus) are rendered LAST, after the document, for two reasons:
  1. recency: the reader's request is the last thing the model sees, not the document's final page;
  2. prompt caching: instructions + document form a stable prefix, so a second summary of the same document with
     different options can reuse OpenAI's cached input tokens (an identical prefix of >= 1,024 tokens).
"""

from collections.abc import Sequence

from app.ai.prompts.common import UNTRUSTED_RULE, escape
from app.ai.prompts.insights import PageLike, document_block

NAME = "summary"
VERSION = "summary.v1"

WORD_BUDGET: dict[str, int] = {"short": 80, "medium": 200, "detailed": 450}

TONES: dict[str, str] = {
    "neutral": "Neutral and factual: plain sentences, no opinions or recommendations.",
    "executive": "For a busy executive: lead with the bottom line, then money, dates, obligations and risks. "
                 "Short bullets are fine.",
    "technical": "For a specialist: keep exact terms, definitions, numbers and section or clause references.",
    "simple": "For a non-expert: everyday words, short sentences, and explain any jargon you keep.",
}

INSTRUCTIONS = f"""You write a summary of ONE document for the reader described in <options>.

Rules:
1. Use only facts stated in the document. No outside knowledge, no opinions, no invented details.
2. Copy numbers, dates, names, amounts, codes and clause numbers exactly as written.
3. Stay within the word limit in <options>.
4. Write in the tone described in <options>.
5. If <options> contains a focus, follow the focus rule written there.
6. If the document tag says coverage="sampled", some pages were left out: say in one short clause that the summary
   is based on excerpts.
7. Output plain markdown: a short paragraph, optionally followed by bullets. No title or heading.
8. {UNTRUSTED_RULE}"""


def focus_rule(focus: str) -> str:
    return f"Prioritize {focus}; if the document does not cover it, say so first."


def render(filename: str, pages: Sequence[PageLike], coverage: str, *, page_count: int, length: str, tone: str,
           focus: str | None) -> str:
    """Document first, options last. `focus` is user text, so it is escaped like document text."""
    doc = document_block(name=filename, pages=pages, page_count=page_count, coverage=coverage)
    options = [f"Length: at most {WORD_BUDGET[length]} words.", f"Tone: {TONES[tone]}"]
    if focus:
        options.append(f"Focus: {focus_rule(escape(focus))}")
    options_block = "<options>\n" + "\n".join(options) + "\n</options>"
    task = "Task: write the summary of the document above, following your rules and the options."
    return f"{doc}\n{options_block}\n{task}"
