"""followups.v1: three follow-up questions shown as chips under an answer.

Designed to be answerable: the model only sees excerpts we already retrieved for this question, preferring the ones
the answer did NOT cite (so a chip leads somewhere new). Not guaranteed — the next question goes through retrieval
and the gate like any other — but in practice the chips rarely lead to "I couldn't find this".
Runs on the fast model after the answer is saved, under a 3-4 s budget; on timeout the chips are simply skipped.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import BaseModel, Field

from app.ai.prompts.common import UNTRUSTED_RULE, escape

NAME = "followups"
VERSION = "followups.v1"
EXCERPT_CHARS = 200

INSTRUCTIONS = (
    "Suggest exactly 3 short follow-up questions the user could ask next about their documents.\n"
    "- Each question must be answerable from the excerpts provided; prefer excerpts the answer did not use.\n"
    "- Do not repeat or rephrase the user's question. One sentence each, at most 15 words.\n"
    "- Write them as the user would ask them, without numbering.\n"
    f"{UNTRUSTED_RULE}"
)


class FollowupQuestions(BaseModel):
    """Structured output of followups.v1 (FakeProvider canned responses use this class name)."""

    questions: list[str] = Field(description="Exactly 3 follow-up questions")


@dataclass(frozen=True)
class Excerpt:
    header: str
    content: str


def render(*, question: str, answer: str, excerpts: Sequence[Excerpt]) -> str:
    blocks = "\n".join(
        f'<excerpt header="{escape(e.header)}">{escape(e.content[:EXCERPT_CHARS])}</excerpt>' for e in excerpts
    )
    return (f"<question>{escape(question)}</question>\n<answer>{escape(answer)}</answer>\n"
            f"<sources>\n{blocks}\n</sources>\n"
            "Suggest 3 follow-up questions answerable from these excerpts.")
