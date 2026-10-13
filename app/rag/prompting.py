"""From retrieved chunks to the answer prompt: number the sources S1..Sk and render answer.v1/v2.

Source ids are assigned per turn in retrieval order (S1 = best RRF score). The same mapping is used for the `meta`
event, for checking the model's citations and for the citation snapshot, so [S3] always means the same chunk.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from app.ai.prompts import answer as answer_prompt
from app.ai.provider import Msg
from app.config import settings
from app.rag.retrieval import RetrievedChunk


@dataclass(frozen=True)
class AnswerPrompt:
    prompt_version: str         # "answer.v2"
    instructions: str
    input: list[Msg]

    def text(self) -> str:
        """Everything sent to the model, for local token estimation when the stream is abandoned."""
        return self.instructions + "\n" + "\n".join(m["content"] for m in self.input)


def number_sources(chunks: Sequence[RetrievedChunk]) -> dict[str, RetrievedChunk]:
    return {f"S{i}": chunk for i, chunk in enumerate(chunks, start=1)}


def to_prompt_sources(numbered: dict[str, RetrievedChunk]) -> list[answer_prompt.PromptSource]:
    return [
        answer_prompt.PromptSource(ref=ref, document=c.filename, location=c.label,
                                   content=f"{c.header}\n{c.content}",
                                   document_date=c.document_date)
        for ref, c in numbered.items()
    ]


def build_answer_prompt(
    *,
    history: Sequence[Msg],
    question: str,
    standalone: str | None,
    numbered: dict[str, RetrievedChunk],
    version: Literal["v1", "v2"] | None = None,
) -> AnswerPrompt:
    prompt_version, instructions, messages = answer_prompt.render(
        version=version or settings.ANSWER_PROMPT_VERSION, history=history, question=question,
        standalone=standalone, sources=to_prompt_sources(numbered), today=settings.today(),
    )
    return AnswerPrompt(prompt_version, instructions, messages)
