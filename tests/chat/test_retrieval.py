"""Hybrid retrieval against the real Postgres (pgvector + full-text). Chunks are inserted directly so each test
controls exactly what is indexed; vectors are FakeProvider's bag-of-words embeddings."""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.fake_provider import fake_vector
from app.config import settings
from app.rag.query_terms import STOPWORDS, extract_terms
from app.rag.retrieval import retrieve
from tests.chat.helpers import insert_document

INCIDENT = "Incident LRK-4471 was a payroll outage on 3 March; the fix shipped the same day."
FILLER = [
    "The incident review process requires a written postmortem within five business days.",
    "Every incident is assigned a severity from one to four by the on-call engineer.",
    "Payroll runs on the last business day of each month for all employees.",
]


async def _retrieve(db: AsyncSession, owner_id: uuid.UUID, doc_ids: list[uuid.UUID], q: str, **kw):
    return await retrieve(db, owner_id=owner_id, document_ids=doc_ids, query_text=q,
                          query_vec=kw.pop("query_vec", fake_vector(q)), **kw)


async def test_identifier_chunk_ranks_first_in_keyword_leg(db: AsyncSession, demo_user_id: uuid.UUID) -> None:
    a = await insert_document(db, demo_user_id, "ops_log.txt", [*FILLER, INCIDENT])
    b = await insert_document(db, demo_user_id, "handbook.txt", FILLER)
    result = await _retrieve(db, demo_user_id, [a, b], "What happened in incident LRK-4471?", mode="keyword")
    assert result.mode == "keyword" and result.id_hit
    top = result.chunks[0]
    assert top.content == INCIDENT
    assert top.lex_rank == 1 and top.id_hit and top.sem_rank is None
    assert all(not c.id_hit for c in result.chunks[1:])
    assert result.terms == ["happened", "incident", "lrk-4471"] and result.id_terms == ["lrk-4471"]


async def test_dotted_clause_number_is_an_identifier_hit(db: AsyncSession, demo_user_id: uuid.UUID) -> None:
    doc = await insert_document(db, demo_user_id, "msa.txt", [
        "9.1 Either party may terminate for material breach.",
        "9.2 Either party may terminate for convenience with 60 days written notice.",
        "10.2 Fees are payable within 30 days.",
    ])
    result = await _retrieve(db, demo_user_id, [doc], "notice under clause 9.2", mode="keyword")
    assert result.chunks[0].content.startswith("9.2 ") and result.chunks[0].id_hit


async def test_hybrid_fuses_both_legs_with_rrf(db: AsyncSession, demo_user_id: uuid.UUID) -> None:
    doc = await insert_document(db, demo_user_id, "ops_log.txt", [*FILLER, INCIDENT])
    result = await _retrieve(db, demo_user_id, [doc], "payroll outage incident LRK-4471")
    assert result.mode == "hybrid"
    assert result.top_similarity is not None and 0 < result.top_similarity <= 1
    top = result.chunks[0]
    assert top.content == INCIDENT and top.sem_rank is not None and top.lex_rank == 1
    for c in result.chunks:                       # the documented arithmetic, chunk by chunk
        expected = (1 / (settings.RRF_K + c.sem_rank) if c.sem_rank else 0) + \
            settings.LEXICAL_WEIGHT * (1 / (settings.RRF_K + c.lex_rank) if c.lex_rank else 0) + \
            (1 / (settings.RRF_K + 1) if c.id_hit else 0)                  # identifier boost
        assert c.rrf == pytest.approx(expected)
    assert [c.rrf for c in result.chunks] == sorted((c.rrf for c in result.chunks), reverse=True)
    assert top.label == "p. 1" and top.filename == "ops_log.txt"


async def test_vector_and_keyword_modes_drop_a_leg(db: AsyncSession, demo_user_id: uuid.UUID) -> None:
    doc = await insert_document(db, demo_user_id, "ops_log.txt", [*FILLER, INCIDENT])
    vector = await _retrieve(db, demo_user_id, [doc], "payroll incident", mode="vector")
    assert vector.mode == "vector" and all(c.lex_rank is None and c.sem_rank for c in vector.chunks)
    keyword = await _retrieve(db, demo_user_id, [doc], "payroll incident", mode="keyword")
    assert keyword.mode == "keyword" and keyword.top_similarity is None
    assert all(c.sem_rank is None and c.lex_rank for c in keyword.chunks)
    no_vec = await _retrieve(db, demo_user_id, [doc], "payroll incident", query_vec=None)
    assert no_vec.mode == "keyword"               # failed query embedding -> keyword only


async def test_empty_term_list_means_vector_only(db: AsyncSession, demo_user_id: uuid.UUID) -> None:
    doc = await insert_document(db, demo_user_id, "ops_log.txt", FILLER)
    result = await _retrieve(db, demo_user_id, [doc], "what is it?")
    assert result.mode == "vector" and result.terms == [] and result.chunks


async def test_per_document_cap_in_multi_document_scope(db: AsyncSession, demo_user_id: uuid.UUID) -> None:
    text_ = "The liability cap is limited to fees paid in the prior twelve months, clause {}."
    a = await insert_document(db, demo_user_id, "a.txt", [text_.format(i) for i in range(6)])
    b = await insert_document(db, demo_user_id, "b.txt", [text_.format(i + 10) for i in range(6)])
    both = await _retrieve(db, demo_user_id, [a, b], "liability cap fees", k=8)
    per_doc = {d: sum(1 for c in both.chunks if c.document_id == d) for d in (a, b)}
    assert max(per_doc.values()) <= settings.PER_DOC_CAP and sum(per_doc.values()) == 2 * settings.PER_DOC_CAP
    single = await _retrieve(db, demo_user_id, [a], "liability cap fees", k=8)
    assert len(single.chunks) == 6                # one document in scope: the cap is k
    capped = await _retrieve(db, demo_user_id, [a, b], "liability cap fees", k=8, per_doc_cap=1)
    assert len(capped.chunks) == 2


async def test_other_owner_chunks_never_returned(
    db: AsyncSession, demo_user_id: uuid.UUID, other_user_id: uuid.UUID
) -> None:
    mine = await insert_document(db, demo_user_id, "mine.txt", FILLER)
    theirs = await insert_document(db, other_user_id, "theirs.txt", [INCIDENT, *FILLER])
    leaked = await _retrieve(db, demo_user_id, [theirs], "incident LRK-4471 payroll")
    assert leaked.chunks == []
    mixed = await _retrieve(db, demo_user_id, [mine, theirs], "incident LRK-4471 payroll")
    assert mixed.chunks and {c.document_id for c in mixed.chunks} == {mine}
    assert not mixed.id_hit


async def test_unembedded_chunks_are_skipped_by_vector_leg(db: AsyncSession, demo_user_id: uuid.UUID) -> None:
    doc = await insert_document(db, demo_user_id, "pending.txt", FILLER, embed=False)
    assert (await _retrieve(db, demo_user_id, [doc], "incident review", mode="vector")).chunks == []
    assert (await _retrieve(db, demo_user_id, [doc], "incident review", mode="keyword")).chunks


async def test_labels_come_from_document_pages(db: AsyncSession, demo_user_id: uuid.UUID) -> None:
    doc = await insert_document(db, demo_user_id, "msa.pdf", [(4, "Termination for convenience needs notice.")],
                                labels={4: "iv"})
    result = await _retrieve(db, demo_user_id, [doc], "termination notice")
    assert result.chunks[0].label == "iv" and result.chunks[0].page_start == 4


async def test_tsquery_accepts_hostile_input(db: AsyncSession) -> None:
    for q in ["a & b | c ! (d) : e* <-> 'f'", "LRK-4471 9.2 v2.0.1 U.S. a-b-c", "--- ... !!!"]:
        terms = extract_terms(q)
        if terms.q_or:
            await db.execute(text("SELECT to_tsquery('english', :q)"), {"q": terms.q_or})


async def test_python_stopwords_are_postgres_stopwords(db: AsyncSession) -> None:
    """Dropping a word Postgres would index would silently lose keyword matches."""
    rows = (await db.execute(text("SELECT w FROM unnest(CAST(:w AS text[])) w "
                                  "WHERE ts_lexize('english_stem', w) <> '{}'"), {"w": sorted(STOPWORDS)})).all()
    assert rows == []


async def test_identifier_match_survives_fusion_against_both_leg_chunks(db: AsyncSession, demo_user_id: uuid.UUID) -> None:
    """Regression (eval case f-section-9-2): a chunk found ONLY by the keyword leg's identifier tier must not be
    outranked by chunks that are merely okay on both legs."""
    filler = [f"Brightline MSA general terms paragraph {i} about services, fees and obligations." for i in range(12)]
    clause = "9.2 Either party may terminate this Agreement for convenience with sixty days written notice."
    doc = await insert_document(db, demo_user_id, "msa.txt", [*filler, clause])
    result = await _retrieve(db, demo_user_id, [doc], "What does section 9.2 of the Brightline MSA say?")
    assert result.chunks[0].content == clause and result.chunks[0].id_hit


async def test_structural_phrases_never_collapse_to_a_single_lexeme(db: AsyncSession) -> None:
    """Every structural phrase we send must stay a real phrase after Postgres parsing. One that collapses to a single
    lexeme ('appendix <-> a' -> 'appendix') matches every chunk containing the bare word."""
    labels = [*"abcdefghijklmnopqrstuvwxyz", "ii", "iv", "x", "9", "9.2"]
    for word in ("appendix", "schedule", "part", "section", "exhibit"):
        for label in labels:
            for phrase in extract_terms(f"{word} {label}").id_terms:
                if "<->" not in phrase:
                    continue
                nodes = await db.scalar(text("SELECT numnode(to_tsquery('english', :q))"), {"q": phrase})
                assert nodes > 1, phrase


async def test_appendix_a_does_not_flag_other_appendices_as_identifier_hits(
    db: AsyncSession, demo_user_id: uuid.UUID
) -> None:
    """Regression: "Appendix A" made Appendix B and C chunks exact identifier hits (and gave them the id boost)."""
    doc = await insert_document(db, demo_user_id, "msa.txt", [
        "Appendix B - Fees. The monthly platform fee is billed in advance.",
        "Appendix C - Automated Review Notice. Reviews run every night.",
        "General terms: this agreement covers services and support.",
    ])
    a = await _retrieve(db, demo_user_id, [doc], "What does Appendix A cover?")
    assert a.id_terms == [] and not a.id_hit and not any(c.id_hit for c in a.chunks)
    c = await _retrieve(db, demo_user_id, [doc], "What does Appendix C cover?")
    assert [ch.content[:10] for ch in c.chunks if ch.id_hit] == ["Appendix C"]
    assert c.chunks[0].content.startswith("Appendix C")
