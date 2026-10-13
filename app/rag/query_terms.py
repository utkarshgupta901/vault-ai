"""Turn a question into keyword-search terms for the lexical leg of hybrid retrieval.

Why a regex instead of `websearch_to_tsquery` / `plainto_tsquery`:
  - plainto_tsquery ANDs every word, so one word the document doesn't contain ("how many PTO days do I get?")
    makes the whole keyword leg return nothing. We OR the terms and let ts_rank_cd reward chunks matching more.
  - TERM_RE only admits letters, digits and inner '.' / '-', so the string we hand to to_tsquery can never contain
    a tsquery operator (& | ! ( ) : * <->) and to_tsquery can never raise on user input.
  - Identifier-like terms (LRK-4471, Q3, 9.2) are pulled out as `id_terms`. The SQL ranks chunks matching them first,
    because an exact identifier is the strongest relevance signal there is and embeddings are bad at it.

Postgres parses each term with the same 'english' config as the index, so stemming ("days" -> "day") and the hyphen
handling ('lrk-4471' -> 'lrk' <-> '-4471') are identical on both sides.
"""

import re
from dataclasses import dataclass

TERM_RE = re.compile(r"[A-Za-z0-9]+(?:[.\-][A-Za-z0-9]+)*")
DOTTED_NUMBER_RE = re.compile(r"\d+(?:\.\d+)+")      # clause / section numbers: 9.2, 4.1.3
MAX_TERMS = 32
# "Appendix C", "section 9", "clause 11.2", "exhibit B", "schedule 2": a structural word + a short label. The label
# alone ("c", "9") is too weak to be an identifier, but the pair names exactly one place in a document, so it joins
# the identifier tier as a PHRASE query ('appendix <-> c' = "appendix" immediately followed by "c"). Both parts come
# from TERM_RE-safe tokens, so the only operator in the string is the '<->' we add ourselves.
STRUCTURAL_RE = re.compile(
    r"\b(appendix|annex|exhibit|schedule|section|clause|article|attachment|part)\s+([a-z]|\d+(?:\.\d+)*|[ivx]{1,5})\b",
    re.IGNORECASE,
)

# PostgreSQL's 'english' stop list (snowball english.stop, 127 words; verified with ts_lexize in
# tests/chat/test_retrieval.py). Postgres would drop these anyway; dropping them here too means the 32-term cap is
# spent on content words, an all-stopword question ("what is it?") falls back to vector-only search, and the
# retrieval debug output shows only the words that can actually match.
STOPWORDS = frozenset("""
i me my myself we our ours ourselves you your yours yourself yourselves he him his himself she her hers herself
it its itself they them their theirs themselves what which who whom this that these those am is are was were be
been being have has had having do does did doing a an the and but if or because as until while of at by for with
about against between into through during before after above below to from up down in out on off over under again
further then once here there when where why how all any both each few more most other some such no nor not only own
same so than too very s t can will just don should now
""".split())


@dataclass(frozen=True)
class QueryTerms:
    terms: list[str]        # lowercased, deduped, stopwords removed, at most MAX_TERMS
    id_terms: list[str]     # identifier-like terms (LRK-4471, 9.2) + structural phrases ('appendix <-> c')

    @property
    def q_or(self) -> str:
        """to_tsquery input matching ANY term: 'pto | day | lrk-4471'. Empty when there are no terms."""
        return " | ".join(self.terms)

    @property
    def q_ids(self) -> str:
        """to_tsquery input matching ANY identifier. Falls back to q_or so the SQL always gets a valid query
        (it is only evaluated when has_ids is true)."""
        return " | ".join(self.id_terms) or self.q_or

    @property
    def has_ids(self) -> bool:
        return bool(self.id_terms)


def is_identifier(term: str) -> bool:
    """LRK-4471, Q3, v2, 2026Q1 (letters AND digits) or a dotted number like 9.2 (a clause reference).
    Plain numbers ("18") and plain words are not identifiers: they are too common to deserve the top tier."""
    has_letter = any(ch.isalpha() for ch in term)
    has_digit = any(ch.isdigit() for ch in term)
    return (has_letter and has_digit) or bool(DOTTED_NUMBER_RE.fullmatch(term))


def extract_terms(question: str) -> QueryTerms:
    terms: list[str] = []
    seen: set[str] = set()
    for raw in TERM_RE.findall(question or ""):
        term = raw.lower()
        if term in seen or term in STOPWORDS:
            continue
        seen.add(term)
        terms.append(term)
        if len(terms) == MAX_TERMS:
            break
    id_terms = [t for t in terms if is_identifier(t)]
    for word, label in STRUCTURAL_RE.findall(question or ""):
        phrase = f"{word.lower()} <-> {label.lower()}"
        if phrase not in id_terms:
            id_terms.append(phrase)
    return QueryTerms(terms=terms, id_terms=id_terms)
