"""The gate (refuse without calling the model) and the confidence badge. Both are plain rules over retrieval and
citation signals, so every badge and every refusal can be explained from numbers stored on the message.

Gate: if the best semantic match is weak (top_similarity < GATE_MIN_SIMILARITY) and no identifier matched, the
documents almost certainly don't cover the question ("what's the weather?"). Answering would cost ~$0.015 and risk a
hallucination, so we send a fixed refusal instead. The identifier exception matters: "status of LRK-4471?" can embed
poorly yet match a chunk exactly. With no sources at all there is nothing to ground on, so that also gates.

Confidence:
  low    - not answered, any citation with weak support, or any factual sentence without a citation;
  high   - >= 1 valid citation, top_similarity >= TAU_HIGH, (a keyword match on a cited chunk or >= 2 valid
           citations), and no invalid citation ids;
  medium - everything else.
TAU_HIGH is calibrated by the eval (25th percentile of top similarity among correct answers).
"""

from collections.abc import Sequence

from app.config import settings
from app.rag.retrieval import RetrievalResult

GATE_REPLY = "I couldn't find this in your documents."
MAX_TITLES = 6


def should_gate(result: RetrievalResult, *, has_query_vector: bool) -> bool:
    if not result.chunks:
        return True
    top = result.top_similarity
    return has_query_vector and top is not None and top < settings.GATE_MIN_SIMILARITY and not result.id_hit


def gate_reply(titles: Sequence[str]) -> str:
    """Deterministic refusal that still helps: tell the user what their documents DO cover."""
    if not titles:
        return GATE_REPLY
    shown = list(titles[:MAX_TITLES])
    more = len(titles) - len(shown)
    listed = ", ".join(shown) + (f" and {more} more" if more > 0 else "")
    return f"{GATE_REPLY} Your documents cover: {listed}."


def confidence(
    *,
    answer_type: str,
    valid_citations: int,
    invalid_citations: int,
    uncited_claims: int,
    any_weak_support: bool,
    top_similarity: float | None,
    lexical_match: bool,
) -> str:
    if answer_type != "answered" or any_weak_support or uncited_claims > 0:
        return "low"
    if (valid_citations >= 1 and top_similarity is not None and top_similarity >= settings.TAU_HIGH
            and (lexical_match or valid_citations >= 2) and invalid_citations == 0):
        return "high"
    return "medium"
