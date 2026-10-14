"""The pre-demo cleanup: the pure planner (evals/demo_library.py) rule by rule, then scripts/pre_demo.py end to end
against the in-memory fake API with three users (dry run, --check exit codes, --apply and its guards,
--create-backups). No compose stack and no model are involved. Every name here is a neutral placeholder."""

import contextlib
import hashlib
import io
import re
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest

import evals.demo_library as planner
from evals import golden
from evals.corpus import (
    BOARD_UPDATE,
    BRIGHTPATH,
    CALL_NOTES,
    CORELINK,
    HANDBOOK,
    LIVE_UPLOADS,
    MSA,
    PUBLIC_DOCS,
    RECEIPT,
    SEED_FILES,
    STRESS_MANUAL,
)
from evals.demo_library import (
    BACKUP_TOPICS,
    EXTRA_REASON,
    DenyTerm,
    KnownFile,
    UserLibrary,
    backup_status,
    fold,
    known_files,
    load_deny_terms,
    plan_cleanup,
    plan_documents,
    title_test_reason,
    user_docs_warning,
)
from tests.evals.fake_api import FakeVaultApi, users_transport
from tests.evals.test_scripts import load_script

NIST = "NIST.AI.100-1.pdf"                  # the approved public document, known by its pinned hash
PUBLIC_SHA = PUBLIC_DOCS[NIST]
USER_DOC = "Field_Report.pdf"               # a real document the user dropped into demo_docs/user/
SECRET = "Unrelated_Notes.md"               # stands in for any non-corpus document
KEYS = {"demo": "vault-demo-key", "other": "vault-other-key", "eval": "vault-eval-key"}
PTO_ANSWER = "New employees get 18 days of PTO, and up to 5 unused days carry over [S1]."


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def body(name: str) -> bytes:
    """Test corpus files hold their own name, so a document uploaded from one has the same sha256."""
    return f"content of {name}".encode()


KNOWN = ([KnownFile(n, sha(body(n)), "seed") for n in SEED_FILES]
         + [KnownFile(n, sha(body(n)), "live") for n in LIVE_UPLOADS]
         + [KnownFile(STRESS_MANUAL, sha(body(STRESS_MANUAL)), "drill"), KnownFile(NIST, PUBLIC_SHA, "public"),
            KnownFile(USER_DOC, sha(body(USER_DOC)), "user", True)])


def doc(name: str, data: bytes | None = None, *, digest: str | None = None) -> dict[str, Any]:
    return {"id": str(uuid.uuid4()), "filename": name,
            "sha256": digest or sha(body(name) if data is None else data), "status": "ready"}


def session(title: str | None, scope: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """scope=None: an all-documents session."""
    return {"id": str(uuid.uuid4()), "title": title, "all_documents": scope is None,
            "document_ids": [d["id"] for d in scope or []]}


def answer(cites: Sequence[dict[str, Any]] = (), retrieved: Sequence[dict[str, Any]] = (), *,
           answer_type: str = "answered", content: str = "") -> dict[str, Any]:
    """An assistant message as GET /messages returns it: citations for `cites`, retrieval candidates for both
    (any dict with id and filename will do, including a document that no longer exists)."""
    return {"role": "assistant", "status": "complete", "answer_type": answer_type, "content": content,
            "citations": [{"ref": f"S{i}", "document_id": d["id"], "filename": d["filename"]}
                          for i, d in enumerate(cites, start=1)],
            "retrieval": {"candidates": [{"document_id": d["id"], "filename": d["filename"]}
                                         for d in [*cites, *retrieved]]}}


def corpus_docs() -> list[dict[str, Any]]:
    return [doc(n) for n in SEED_FILES] + [doc(NIST, digest=PUBLIC_SHA), doc(USER_DOC)]


def decisions(plan: Any) -> dict[str, tuple[bool, str]]:
    return {d.filename: (d.delete, d.reason) for d in plan}


# --- known files ----------------------------------------------------------------------------------------------------
def test_known_files_hash_the_corpus_and_the_user_folder(tmp_path: Path) -> None:
    for name in [*SEED_FILES[1:], *LIVE_UPLOADS, STRESS_MANUAL, "Extra_Notes.md", "readme.xyz"]:
        (tmp_path / name).write_bytes(body(name))
    (tmp_path / "user").mkdir()
    (tmp_path / "user" / USER_DOC).write_bytes(body(USER_DOC))
    known = {k.name: k for k in known_files(tmp_path)}
    assert known[HANDBOOK].sha256 is None and known[HANDBOOK].role == "seed"       # not generated: name only
    assert known[BOARD_UPDATE].sha256 == sha(body(BOARD_UPDATE))
    assert {known[n].role for n in LIVE_UPLOADS} == {"live"} and known[STRESS_MANUAL].role == "drill"
    assert known["Extra_Notes.md"].role == "extra" and "readme.xyz" not in known  # unsupported suffix: ignored
    assert known[USER_DOC].role == "user" and known[USER_DOC].in_user_dir
    assert known[NIST] == KnownFile(NIST, PUBLIC_SHA, "public", False)            # pinned, no folder needed


def test_the_public_document_is_listed_as_in_the_user_folder_when_it_is_there(tmp_path: Path,
                                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "user").mkdir()
    (tmp_path / "user" / NIST).write_bytes(body(NIST))
    monkeypatch.setattr(planner, "PUBLIC_DOCS", {NIST: sha(body(NIST))})    # these bytes are "the" PDF here
    assert [k for k in known_files(tmp_path) if k.name == NIST] == [KnownFile(NIST, sha(body(NIST)), "public", True)]
    monkeypatch.undo()                                                           # other bytes under that name:
    assert {k.role for k in known_files(tmp_path) if k.name == NIST} == {"public", "user"}   # a user document


def test_the_public_document_is_kept_without_any_user_folder(tmp_path: Path) -> None:
    known = known_files(tmp_path)                                    # a fresh clone: no demo_docs/user/ at all
    result, warnings = plan_documents("demo", [doc(NIST, digest=PUBLIC_SHA)], known, demo=True)
    assert decisions(result) == {NIST: (False, "approved public document")} and warnings == []
    renamed, _ = plan_documents("demo", [doc("nist-rmf.pdf", digest=PUBLIC_SHA)], known, demo=True)
    assert not renamed[0].delete                                     # bytes win over names


def test_user_docs_warning(tmp_path: Path) -> None:
    assert "does not exist here" in user_docs_warning(tmp_path)
    (tmp_path / "user").mkdir()
    (tmp_path / "user" / ".deny_terms").write_text("x\n", encoding="utf-8")   # not a document
    assert "is empty" in user_docs_warning(tmp_path) and NIST in user_docs_warning(tmp_path)
    (tmp_path / "user" / USER_DOC).write_bytes(b"real")
    assert user_docs_warning(tmp_path) is None


# --- documents ------------------------------------------------------------------------------------------------------
def test_demo_user_deletes_unknown_documents_and_live_uploads() -> None:
    docs = corpus_docs() + [doc(SECRET), doc(CALL_NOTES), doc(RECEIPT)]
    result, warnings = plan_documents("demo", docs, KNOWN, demo=True)
    deleted = {d.filename: d.role for d in result if d.delete}
    assert deleted == {SECRET: None, CALL_NOTES: "live", RECEIPT: "live"}
    assert {d.filename for d in result if not d.delete} == {*SEED_FILES, NIST, USER_DOC}
    assert decisions(result)[SECRET] == (True, "not a demo corpus file") and warnings == []


def test_other_users_delete_unknown_documents_only() -> None:
    docs = corpus_docs() + [doc(SECRET), doc(CALL_NOTES)]
    result, _ = plan_documents("eval", docs, KNOWN, demo=False)
    assert [d.filename for d in result if d.delete] == [SECRET]


def test_a_regenerated_corpus_file_is_kept_by_filename_with_a_warning() -> None:
    regenerated = doc(HANDBOOK, data=b"regenerated PDF, new bytes")
    renamed = doc("handbook (1).pdf", data=body(MSA))                          # corpus bytes under another name
    result, warnings = plan_documents("demo", [regenerated, renamed], KNOWN, demo=True)
    assert decisions(result) == {HANDBOOK: (False, "seed corpus"), "handbook (1).pdf": (False, "seed corpus")}
    assert len(warnings) == 1 and HANDBOOK in warnings[0] and "bytes differ" in warnings[0]
    no_bytes = [KnownFile(HANDBOOK, None, "seed")]                             # file not on disk here
    kept, warnings = plan_documents("demo", [regenerated], no_bytes, demo=True)
    assert not kept[0].delete and "not in demo_docs here" in warnings[0]


def test_drill_files_and_duplicate_copies_are_kept_with_warnings() -> None:
    docs = [doc(STRESS_MANUAL), doc(HANDBOOK), doc(HANDBOOK, data=b"older copy")]
    result, warnings = plan_documents("demo", docs, KNOWN, demo=True)
    assert not any(d.delete for d in result)
    assert any(STRESS_MANUAL in w and "does not pre-load" in w for w in warnings)
    assert any(f"{HANDBOOK} is in the library 2 times" in w for w in warnings)
    assert plan_documents("eval", docs[:1], KNOWN, demo=False)[1] == []       # other users: no opinion


def test_a_file_dropped_into_demo_docs_is_kept_but_blocks_the_snapshot() -> None:
    dropped = KnownFile("Dropped_Notes.md", sha(b"dropped"), "extra")
    for user in ("demo", "eval"):
        lib = UserLibrary(user, corpus_docs() + [doc("Dropped_Notes.md", b"dropped")], [])
        plan = plan_cleanup([lib], [*KNOWN, dropped])
        assert decisions(plan.untrusted) == {"Dropped_Notes.md": (False, EXTRA_REASON)}
        assert plan.empty and plan.blocks_snapshot                  # nothing to delete, and still not clean


# --- sessions -------------------------------------------------------------------------------------------------------
def demo_library(docs: list[dict[str, Any]], *sessions: tuple[dict[str, Any], list[dict[str, Any]]]) -> UserLibrary:
    return UserLibrary("demo", docs, [s for s, _ in sessions], {s["id"]: msgs for s, msgs in sessions})


def session_plan(lib: UserLibrary, **kwargs: Any) -> dict[str, Any]:
    return {s.title: s for s in plan_cleanup([lib], KNOWN, **kwargs).sessions}


def test_sessions_that_use_a_deleted_document_are_deleted() -> None:
    docs = corpus_docs()
    secret, notes, handbook = doc(SECRET), doc(CALL_NOTES), docs[0]
    lib = demo_library(docs + [secret, notes],
                       (session("Old notes", [secret]), []),
                       (session("Cited secret"), [answer([secret])]),
                       (session("Retrieved secret"), [answer([handbook], [secret])]),
                       (session("Cited notes"), [answer([notes])]),
                       (session("Notes in scope", [notes]), []),
                       (session("Retrieved notes"), [answer([handbook], [notes])]),
                       (session("PTO rehearsal"), [answer([handbook])]))
    plan = session_plan(lib)
    assert plan["Old notes"].delete and plan["Old notes"].leaks
    assert plan["Old notes"].reasons == (f"{SECRET} (scope; deleting now)",)
    assert plan["Cited secret"].reasons == (f"{SECRET} (cited; deleting now)",) and plan["Cited secret"].leaks
    assert plan["Retrieved secret"].reasons == (f"{SECRET} (retrieved; deleting now)",)
    assert plan["Retrieved secret"].leaks
    # A live upload is corpus text: in scope or cited -> the session goes (broken citations), but nothing leaks;
    # merely retrieved -> the session stays (it is rehearsal history that /metrics/chat needs).
    assert plan["Cited notes"].delete and not plan["Cited notes"].leaks
    assert plan["Notes in scope"].delete and not plan["Notes in scope"].leaks
    assert not plan["Retrieved notes"].delete and not plan["PTO rehearsal"].delete


def test_sessions_left_over_from_earlier_deletes() -> None:
    docs = corpus_docs()
    gone_plan = {"id": str(uuid.uuid4()), "filename": "Old_Plan.md"}
    gone_smoke = {"id": str(uuid.uuid4()), "filename": "smoke-test.txt"}
    gone_handbook = {"id": str(uuid.uuid4()), "filename": HANDBOOK}           # an earlier copy of a corpus file
    lib = demo_library(docs,
                       (session("Earlier chat", []), [answer([gone_plan])]),    # scope cascaded away
                       (session("Q&A"), [answer([], [gone_smoke])]),
                       (session("Old PTO"), [answer([gone_handbook])]))
    plan = plan_cleanup([lib], KNOWN)
    by_title = {s.title: s for s in plan.sessions}
    assert by_title["Earlier chat"].reasons == ("Old_Plan.md (cited; deleted earlier)",
                                                "empty scope: its documents were deleted")
    assert by_title["Earlier chat"].leaks
    assert by_title["Q&A"].delete and by_title["Q&A"].reasons == ("smoke-test.txt (retrieved; deleted earlier)",)
    assert not by_title["Old PTO"].delete                                    # corpus text: kept, with a warning
    assert any("Old PTO" in w and "source deleted" in w for w in plan.warnings)
    empty = plan_cleanup([demo_library(docs, (session("Orphan", []), []))], KNOWN).sessions[0]
    assert empty.delete and not empty.leaks


@pytest.mark.parametrize(("title", "reason"), [
    ("smoke test", 'title has "smoke test"'), ("Smoke tests", 'title has "smoke test"'),
    ("UI testing", 'title has "ui test"'), ("test", 'title is "test"'), ("  Tests ", 'title is "tests"'),
    ("What does the stress test manual say about retries?", None), ("Draft memo test", None),
    ("pre-test run", None), ("Contest prep", None), ("Backup: PTO", None), (None, None)])
def test_test_titles(title: str | None, reason: str | None) -> None:
    assert title_test_reason(title, ("smoke test", "ui test")) == reason


def test_test_title_phrases_can_be_replaced() -> None:
    lib = demo_library(corpus_docs(), (session("UI test"), []), (session("my scratch chat"), []),
                       (session("TEST"), []))
    plan = session_plan(lib, test_titles=["scratch"])
    assert not plan["UI test"].delete and plan["my scratch chat"].reasons == ('title has "scratch"',)
    assert plan["TEST"].reasons == ('title is "test"',)                     # a bare "test" title always goes


# --- deny terms: chats that only MENTION a non-corpus document --------------------------------------------------------
def test_deny_terms_file(tmp_path: Path) -> None:
    assert load_deny_terms(tmp_path) == ([], [])                             # no file: no terms, no warning
    (tmp_path / "user").mkdir()
    (tmp_path / "user" / ".deny_terms").write_text(
        "# what no demo chat may mention\n\nProject_Nightjar\n  Old Codename  \nq3\n", encoding="utf-8")
    terms, warnings = load_deny_terms(tmp_path)
    assert [t.text for t in terms] == ["project nightjar", "old codename"]
    assert all(t.from_file and "nightjar" not in t.label.casefold() for t in terms)   # reasons never echo it
    assert warnings == [".deny_terms line 5 ignored: under 4 characters, it would match nearly every chat"]


def test_a_chat_that_mentions_a_deny_term_anywhere_is_deleted_and_leaks() -> None:
    docs = corpus_docs()
    term = DenyTerm(fold("Project Nightjar"), "deny term (line 1 of .deny_terms)", True)
    q = {"role": "user", "content": "Tell me about the handbook"}
    lib = demo_library(docs,
                       (session("PROJECT_NIGHTJAR budget"), []),
                       (session("Asked"), [{"role": "user", "content": "What is project nightjar?"}]),
                       (session("Rewritten"), [q, {**answer([docs[0]]), "standalone_query": "nightjar? project "
                                                   "Nightjar timeline"}]),
                       (session("Suggested"), [q, {**answer([docs[0]]), "followups": ["And Project Nightjar?"]}]),
                       (session("Plural"), [{"role": "user", "content": "project nightjars"}]),   # whole words only
                       (session("PTO rehearsal"), [q, answer([docs[0]], content=PTO_ANSWER)]))
    plan = session_plan(lib, deny_terms=[term])
    for title in ("PROJECT_NIGHTJAR budget", "Asked", "Rewritten", "Suggested"):
        assert plan[title].reasons == ("mentions deny term (line 1 of .deny_terms)",) and plan[title].leaks
    assert not plan["Plural"].delete and not plan["PTO rehearsal"].delete


def test_a_question_about_a_document_that_never_retrieved_it_is_caught_by_its_name() -> None:
    """The document was still processing (not_found from the corpus) or the turn failed (no retrieval at all): the
    only trace is the question text, which is also the session title on screen."""
    docs = corpus_docs()
    secret = doc(SECRET)
    q = "Summarise unrelated notes.md: how is the pricing set?"
    failed_turn = [{"role": "user", "content": q},
                   {"role": "assistant", "status": "failed", "content": "", "citations": [], "retrieval": None}]
    not_found = [{"role": "user", "content": q}, answer([], [docs[0]], answer_type="not_found")]
    chats = [(session(q), failed_turn), (session(q[:30], [docs[0]]), not_found)]
    plan = plan_cleanup([demo_library(docs + [secret], *chats)], KNOWN)
    assert [s.reasons for s in plan.sessions] == [('mentions "Unrelated_Notes"',)] * 2
    assert all(s.leaks for s in plan.sessions)
    # Deleted outside this tool, the name is gone with it: only demo_docs/user/.deny_terms still catches the chats.
    assert plan_cleanup([demo_library(docs, *chats)], KNOWN).empty
    term = DenyTerm(fold("Unrelated Notes"), "deny term (line 1 of .deny_terms)", True)
    assert plan_cleanup([demo_library(docs, *chats)], KNOWN, deny_terms=[term]).blocks_snapshot


def test_a_name_deleted_earlier_still_catches_chats_in_any_vault() -> None:
    docs = corpus_docs()
    gone = {"id": str(uuid.uuid4()), "filename": "Old_Plan.md"}
    demo = demo_library(docs, (session("Cited it"), [answer([gone])]))
    eval_user = UserLibrary("eval", docs, [s := session("Asked about the old plan")],
                            {s["id"]: [{"role": "user", "content": "What did OLD_PLAN say?"}]})
    plan = plan_cleanup([demo, eval_user], KNOWN)
    assert [t.text for t in plan.terms] == ["old plan"]
    assert {(s.user, s.reasons) for s in plan.sessions} == {("demo", ("Old_Plan.md (cited; deleted earlier)",)),
                                                            ("eval", ('mentions "Old_Plan"',))}


def test_short_or_corpus_like_names_are_not_used_as_terms() -> None:
    docs = corpus_docs()
    lib = demo_library(docs + [doc("notes.md"), doc("x1.txt")],
                       (session("Vendor call notes recap"), [answer([docs[0]])]))
    plan = plan_cleanup([lib], KNOWN)
    assert plan.terms == [] and not plan.sessions[0].delete       # "notes" is part of Vendor_Call_Notes
    assert sum("add a term for it to demo_docs/user/.deny_terms" in w for w in plan.warnings) == 2


def test_a_lone_word_or_a_corpus_like_name_is_not_a_term_in_every_vault() -> None:
    """A name term is checked in every vault: "Budget" would delete the rehearsals that ask about the board update's
    budget, and "Q3 Board Update" (a renamed corpus copy, deleted earlier) every chat that names that document."""
    docs, eval_docs = corpus_docs(), corpus_docs()
    gone_copy = {"id": str(uuid.uuid4()), "filename": "Q3 Board Update.pdf"}
    demo = demo_library(docs + [doc("Budget.pdf")],
                        (session("Old copy"), [answer([gone_copy])]),
                        (session("Board rehearsal"), [{"role": "user", "content": "What does the Q3 board update say "
                                                       "about the budget?"}, answer([docs[1]])]))
    eval_user = UserLibrary("eval", eval_docs, [e := session("eval hiring")],
                            {e["id"]: [{"role": "user", "content": "Is the hiring budget on track?"},
                                       answer([eval_docs[1]])]})
    plan = plan_cleanup([demo, eval_user], KNOWN)
    assert plan.terms == []
    assert [(s.user, s.title) for s in plan.session_deletes] == [("demo", "Old copy")]   # it cited the copy: goes
    assert sum("a single word or made of a corpus filename's words" in w for w in plan.warnings) == 2


# --- the whole plan -------------------------------------------------------------------------------------------------
def test_a_clean_vault_gives_an_empty_plan() -> None:
    docs = corpus_docs()
    demo = demo_library(docs, (session("Backup: PTO"), [answer([docs[0]], content=PTO_ANSWER)]),
                        (session("Rehearsal"), []))
    other = UserLibrary("other", [], [])
    eval_user = UserLibrary("eval", corpus_docs(), [session("eval f-pto-days")])
    plan = plan_cleanup([demo, other, eval_user], KNOWN)
    assert plan.empty and not plan.blocks_snapshot and plan.warnings == []
    assert len(plan.documents) == 14 and len(plan.sessions) == 3


def test_what_blocks_a_snapshot() -> None:
    docs = corpus_docs()
    untidy = plan_cleanup([demo_library(docs, (session("UI test"), [answer([docs[0]])]))], KNOWN)
    assert not untidy.empty and not untidy.blocks_snapshot                # a test title over corpus docs: no leak
    assert plan_cleanup([demo_library(docs + [doc(CALL_NOTES)])], KNOWN).blocks_snapshot   # any document delete
    gone = {"id": str(uuid.uuid4()), "filename": SECRET}
    leftover = plan_cleanup([demo_library(docs, (session("Chat"), [answer([gone])]))], KNOWN)
    assert leftover.doc_deletes == [] and leftover.blocks_snapshot          # the answer text still quotes it


# --- Backup sessions --------------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def cases() -> dict[str, golden.Case]:
    return load_script("pre_demo").backup_cases()


def test_backup_status_scores_stored_answers_against_the_golden_case(cases: dict[str, golden.Case]) -> None:
    handbook = doc(HANDBOOK)
    pto, stale, wrong, stipend, proposals, deleted, rehearsal = (
        session("Backup: PTO"), session("Backup: PTO (old)"), session("Backup: PTO v2"),
        session("backup - remote stipend"), session("Backup 3"), session("Backup: proposals"),
        session("PTO rehearsal"))
    proposals_answer = ("BrightPath costs $600,000 over three years [S1], CoreLink $665,000 [S2]; CoreLink's SLA is "
                        "stronger at 99.9% [S2].")
    messages = {pto["id"]: [answer([handbook], content=PTO_ANSWER)],
                stale["id"]: [answer([handbook], content=PTO_ANSWER)],
                wrong["id"]: [answer([handbook], content="New employees get 15 days [S1].")],
                stipend["id"]: [answer([], answer_type="not_found")],
                proposals["id"]: [{"role": "user", "content": cases["m-proposals-cost-sla"].question.upper()},
                                  answer([doc(BRIGHTPATH), doc(CORELINK)], content=proposals_answer)],
                rehearsal["id"]: [answer([handbook], content=PTO_ANSWER)]}
    messages[stale["id"]][0]["citations"][0]["source_deleted"] = True
    statuses = backup_status([stale, wrong, pto, stipend, proposals, deleted, rehearsal], messages, cases,
                             exclude=[deleted["id"]])
    by_topic = {s.topic.topic: s for s in statuses}
    assert by_topic["PTO"].session is pto and by_topic["PTO"].answered      # the passing one comes first
    assert {c.session["title"]: c.detail for c in by_topic["PTO"].failing} == {
        "Backup: PTO (old)": "no cited answer stored",                     # its only source was deleted
        "Backup: PTO v2": "fails f-pto-days: all_of"}                      # cited, but wrong
    assert by_topic["stipend"].session is stipend and not by_topic["stipend"].answered
    assert by_topic["proposals"].session is proposals and by_topic["proposals"].answered   # matched by question
    none = backup_status([deleted], {}, cases, exclude=[deleted["id"]])
    assert [s.session for s in none] == [None, None, None]


def test_every_answer_a_backup_shows_must_pass(cases: dict[str, golden.Case]) -> None:
    """Opening a Backup session shows all of its answers: a wrong one is not excused by a passing one next to it,
    and a passing reply to another question does not count for the topic."""
    handbook, question = doc(HANDBOOK), cases["f-pto-days"].question
    wrong = "New employees get 15 days of PTO [S1]."

    def pto(*messages: dict[str, Any]) -> planner.BackupStatus:
        s = session("Backup: PTO")
        return backup_status([s], {s["id"]: list(messages)}, cases)[0]

    newer_wrong = pto(answer([handbook], content=PTO_ANSWER), answer([handbook], content=wrong))
    assert not newer_wrong.answered and newer_wrong.failing[0].detail == "fails f-pto-days: all_of (1 of 2 answers)"
    elsewhere = pto({"role": "user", "content": question}, answer([handbook], content=wrong),
                    {"role": "user", "content": "And for contractors?"}, answer([handbook], content=PTO_ANSWER))
    assert elsewhere.failing[0].detail == "fails f-pto-days: all_of"        # only the golden question's reply counts
    uncited = pto(answer([handbook], content=PTO_ANSWER), answer([], answer_type="not_found"))
    assert uncited.failing[0].detail == "an answer is not cited (1 of 2 answers)"
    retried = pto({"id": "q1", "role": "user", "content": question}, {"role": "user", "content": "Thanks"},
                  {**answer([handbook], content=PTO_ANSWER), "reply_to_id": "q1"})
    assert retried.answered                          # reply_to_id, not the message before it, names the question


def test_backup_topics_are_demo_cases_in_the_golden_set(cases: dict[str, golden.Case]) -> None:
    every = {c.id: c for c in golden.load(starters=None).cases}
    assert all(every[t.case_id].is_demo for t in BACKUP_TOPICS)
    assert cases[BACKUP_TOPICS[0].case_id] == every["f-pto-days"]


# --- the script against a fake stack ----------------------------------------------------------------------------------
@pytest.fixture
def docs_dir(tmp_path: Path) -> Path:
    folder = tmp_path / "demo_docs"
    (folder / "user").mkdir(parents=True)
    for name in [*SEED_FILES, *LIVE_UPLOADS, STRESS_MANUAL]:
        (folder / name).write_bytes(body(name))
    (folder / "user" / USER_DOC).write_bytes(body(USER_DOC))
    return folder


def seeded(api: FakeVaultApi, *, user_doc: bool = True) -> dict[str, dict[str, Any]]:
    docs = {n: api.add_document(n, body(n)) for n in [*SEED_FILES, *([USER_DOC] if user_doc else [])]}
    return {**docs, NIST: api.add_document(NIST, sha256=PUBLIC_SHA)}


def cite(*docs: dict[str, Any], content: str = "") -> dict[str, Any]:
    return answer(list(docs), content=content)


@pytest.fixture
def stack() -> dict[str, FakeVaultApi]:
    """A used stack: the demo vault holds a non-corpus document and a pre-loaded live upload, chats over them and a
    test chat next to a rehearsal and one passing Backup; the eval vault has one stray upload; `other` is empty."""
    demo, other, eval_user = FakeVaultApi(), FakeVaultApi(), FakeVaultApi()
    corpus = seeded(demo)
    secret, notes = demo.add_document(SECRET), demo.add_document(CALL_NOTES, body(CALL_NOTES))
    demo.add_session("Old notes", [secret["id"]], [cite(secret)])
    demo.add_session("Old notes (after fix)", None, [cite(secret)])
    demo.add_session("UI test", None, [cite(corpus[HANDBOOK])])
    demo.add_session("Vendor call rehearsal", None, [cite(notes)])
    demo.add_session("PTO rehearsal", None, [cite(corpus[HANDBOOK])])
    demo.add_session("Backup: PTO", None, [cite(corpus[HANDBOOK], content=PTO_ANSWER)])
    eval_corpus = seeded(eval_user)
    eval_user.add_document("stray.txt")
    eval_user.add_session("eval f-pto-days", None, [cite(eval_corpus[HANDBOOK])])
    return {"demo": demo, "other": other, "eval": eval_user}


def run(stack: dict[str, FakeVaultApi], docs_dir: Path, *args: str, keys: dict[str, str] = KEYS) -> int:
    transport = users_transport({keys[name]: api for name, api in stack.items()})
    return load_script("pre_demo").main(["--api", "http://vault.test", "--docs-dir", str(docs_dir), *args],
                                        transport=transport)


def reviewed_plan(stack: dict[str, FakeVaultApi], docs_dir: Path, *args: str) -> str | None:
    """The plan id a dry run prints in its apply command, read without printing it: the tests apply the plan a dry
    run shows, the way the operator copies its command. None when the dry run stops before a plan."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        run(stack, docs_dir, *args)
    found = re.search(r"--expect-plan ([0-9a-f]{12})", out.getvalue())
    return found.group(1) if found else None


def apply(stack: dict[str, FakeVaultApi], docs_dir: Path, docs: int, sessions: int, *args: str,
          plan: str | None = None) -> int:
    """--apply with these delete counts and the plan id of a dry run made just now (or `plan`, one made earlier)."""
    plan = plan or reviewed_plan(stack, docs_dir, *args)
    return run(stack, docs_dir, "--apply", "--expect-docs", str(docs), "--expect-sessions", str(sessions),
               *(["--expect-plan", plan] if plan else []), *args)


def writes(stack: dict[str, FakeVaultApi]) -> list[str]:
    return [c for api in stack.values() for c in api.calls if not c.startswith("GET ")]


def titles(api: FakeVaultApi) -> set[str]:
    return {s["title"] for s in api.sessions.values()}


def test_dry_run_prints_the_plan_and_the_command_and_changes_nothing(stack: dict[str, FakeVaultApi], docs_dir: Path,
                                                                     capsys: pytest.CaptureFixture[str]) -> None:
    assert run(stack, docs_dir) == 0
    out = capsys.readouterr().out
    assert writes(stack) == []
    assert f"Trusted real documents ({docs_dir / 'user'}): {USER_DOC} {sha(body(USER_DOC))[:8]}" in out
    assert "Deny terms (title, messages, rewritten query, follow-ups): 0 from" in out
    assert ('plus the names of 1 non-corpus documents (deleting now or deleted earlier): "Unrelated_Notes"' in out)
    assert "stray.txt: its name is too short, a single word or made of a corpus filename's words" in out
    assert "demo: delete 2 of 9 documents" in out and "demo: delete 4 of 6 sessions" in out
    assert f"DELETE  {SECRET}" in out and f"DELETE  {CALL_NOTES}" in out
    assert "approved public document" in out and f"keep    {USER_DOC}" in out
    assert "live upload: the demo uploads it on stage" in out
    assert f"{SECRET} (scope, cited; deleting now)" in out
    assert "keep    PTO rehearsal" in out and "keep    Backup: PTO" in out
    assert "eval: delete 1 of 8 documents" in out and "DELETE  stray.txt" in out
    assert "other: no documents" in out and "other: no sessions" in out
    assert "Backup sessions (demo): 1 of 3 pass their golden case" in out and "MISSING stipend" in out
    assert "passes f-pto-days" in out
    assert "Plan: delete 3 of 17 documents and 4 of 7 sessions." in out
    assert re.search(r"To execute exactly this plan \([0-9a-f]{12}; refused if anything it deletes changes\)", out)
    assert re.search(f"uv run python scripts/pre_demo.py --api http://vault.test --docs-dir {re.escape(str(docs_dir))} "
                     "--apply --expect-docs 3 --expect-sessions 4 --expect-plan [0-9a-f]{12}\n", out)


def test_the_apply_command_uses_make_when_the_makefile_can_express_it() -> None:
    pre_demo = load_script("pre_demo")
    plan = plan_cleanup([demo_library(corpus_docs() + [doc(SECRET)], (session("UI test"), []))], KNOWN)

    def command(*argv: str) -> str:
        args = pre_demo.parser().parse_args(argv)
        args.test_titles = tuple(args.test_titles or pre_demo.DEFAULT_TEST_TITLES)
        return pre_demo.apply_command(args, list(argv), plan, "0123456789ab")

    assert command() == "make pre-demo APPLY=1 EXPECT_DOCS=1 EXPECT_SESSIONS=1 EXPECT_PLAN=0123456789ab"
    assert command("--api", "http://x:9000", "--no-user-docs", "--apply", "--expect-docs", "7", "--create-backups") \
        == ("make pre-demo APPLY=1 EXPECT_DOCS=1 EXPECT_SESSIONS=1 EXPECT_PLAN=0123456789ab API=http://x:9000 "
            "NO_USER_DOCS=1 BACKUPS=1")
    assert command("--docs-dir", "/a b", "--apply", "--expect-docs=7", "--expect-sessions", "9",
                   "--expect-plan=fedcba987654") == ("uv run python scripts/pre_demo.py --docs-dir '/a b' --apply "
                                                     "--expect-docs 1 --expect-sessions 1 --expect-plan 0123456789ab")


def test_check_fails_while_non_corpus_content_is_left(stack: dict[str, FakeVaultApi], docs_dir: Path,
                                                      capsys: pytest.CaptureFixture[str],
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_golden(**_: Any) -> None:
        raise ValueError("invalid golden set")

    monkeypatch.setattr(golden, "load", broken_golden)       # --check never needs the golden set
    assert run(stack, docs_dir, "--check") == 1
    out = capsys.readouterr().out
    assert ("non-corpus content is still in the vault (3 documents, 0 untrusted demo_docs files, 2 sessions that "
            "use or mention one)") in out
    assert "Backup sessions" not in out and writes(stack) == []


def test_check_passes_on_a_clean_vault_even_with_test_chats(docs_dir: Path,
                                                            capsys: pytest.CaptureFixture[str]) -> None:
    demo = FakeVaultApi()
    corpus = seeded(demo)
    clean = {"demo": demo, "other": FakeVaultApi(), "eval": FakeVaultApi()}
    assert run(clean, docs_dir, "--check") == 0
    assert "only demo corpus content is left" in capsys.readouterr().out
    demo.add_session("UI test", None, [cite(corpus[HANDBOOK])])
    assert run(clean, docs_dir, "--check") == 0                 # untidy, not a leak: the snapshot may go ahead
    assert "untidy, not a leak" in capsys.readouterr().out


def test_check_fails_on_a_dropped_file_and_on_a_mention(docs_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    demo = FakeVaultApi()
    seeded(demo)
    clean = {"demo": demo, "other": FakeVaultApi(), "eval": FakeVaultApi()}
    (docs_dir / "Dropped_Notes.md").write_bytes(b"dropped")
    demo.add_document("Dropped_Notes.md", b"dropped")
    assert run(clean, docs_dir, "--check") == 1
    out = capsys.readouterr().out
    assert "in demo_docs/ but not named in evals/corpus.py (a vault copy blocks the snapshot): Dropped_Notes.md" in out
    assert "1 untrusted demo_docs files" in out
    (docs_dir / "Dropped_Notes.md").unlink()
    demo.docs.clear()
    seeded(demo)
    (docs_dir / "user" / ".deny_terms").write_text("# placeholder codename\nProject Nightjar\n", encoding="utf-8")
    demo.add_session("Rehearsal", None, [{"role": "user", "content": "Is Project_Nightjar on track?"}])
    assert run(clean, docs_dir, "--check") == 1
    out = capsys.readouterr().out
    assert "Deny terms (title, messages, rewritten query, follow-ups): 1 from" in out
    assert "mentions deny term (line 2 of .deny_terms)" in out and "nightjar" not in out.casefold()   # not echoed


def test_check_exit_code_2_when_any_vault_cannot_be_read(stack: dict[str, FakeVaultApi], docs_dir: Path,
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    assert run(stack, docs_dir, "--check", "--key", "not-a-key") == 2
    assert "cannot check the vaults at http://vault.test: GET /api/v1/documents -> 401" in capsys.readouterr().err
    # Another user's key rejected (rotated): the dry run warns and goes on, but the snapshot gate may not pass a
    # vault it did not read, whatever that vault holds.
    stack["eval"].add_document(SECRET)
    rotated = {**KEYS, "eval": "rotated-eval-key"}
    assert run(stack, docs_dir, "--check", keys=rotated) == 2
    assert "cannot check the vaults: eval: API key rejected" in capsys.readouterr().err

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    def not_the_vault(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>some other service</html>")

    main = load_script("pre_demo").main
    for handler in (down, not_the_vault):
        assert main(["--check", "--docs-dir", str(docs_dir)], transport=httpx.MockTransport(handler)) == 2
    assert "cannot check the vaults at http://localhost:8000: JSONDecodeError" in capsys.readouterr().err


def test_another_user_with_a_rejected_key_is_skipped_by_the_dry_run(stack: dict[str, FakeVaultApi], docs_dir: Path,
                                                                    capsys: pytest.CaptureFixture[str]) -> None:
    assert run(stack, docs_dir, "--others", "ghost=wrong-key") == 0
    out = capsys.readouterr().out
    assert "ghost: API key rejected, this user was not checked" in out and "eval:" not in out


def test_apply_refuses_without_user_documents_unless_told(stack: dict[str, FakeVaultApi], docs_dir: Path,
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    (docs_dir / "user" / USER_DOC).unlink()          # an EMPTY user/ (the old advice) is no confirmation either
    assert apply(stack, docs_dir, 3, 4) == 2
    assert "is empty" in capsys.readouterr().err
    (docs_dir / "user").rmdir()
    assert run(stack, docs_dir, "--check") == 1
    out = capsys.readouterr().out
    assert out.index("does not exist here") < out.index("demo: delete")      # before the plan it explains
    assert f"DELETE  {USER_DOC}" in out and "approved public document" in out
    assert apply(stack, docs_dir, 4, 4) == 2
    assert "pass --docs-dir; if this deploy really has no real document" in capsys.readouterr().err
    assert writes(stack) == []


def test_no_user_docs_keeps_the_public_document(docs_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    demo = FakeVaultApi()
    seeded(demo, user_doc=False)
    clean = {"demo": demo, "other": FakeVaultApi(), "eval": FakeVaultApi()}
    assert apply(clean, docs_dir, 0, 0, "--no-user-docs") == 2               # contradicts the folder's content
    assert f"--no-user-docs, but {docs_dir / 'user'} holds {USER_DOC}" in capsys.readouterr().err
    (docs_dir / "user" / USER_DOC).unlink()
    assert run(clean, docs_dir, "--no-user-docs") == 0
    out = capsys.readouterr().out
    assert "none (--no-user-docs)" in out and "does not exist" not in out
    assert "make pre-demo" not in out and "--no-user-docs --apply --expect-docs 0 --expect-sessions 0" in out
    assert apply(clean, docs_dir, 0, 0, "--no-user-docs") == 0
    assert NIST in {d["filename"] for d in demo.docs.values()} and writes(clean) == []


def test_apply_needs_the_reviewed_counts_and_plan_id(stack: dict[str, FakeVaultApi], docs_dir: Path,
                                                     capsys: pytest.CaptureFixture[str]) -> None:
    needs = "refusing to apply, nothing was changed: --apply needs --expect-docs, --expect-sessions and --expect-plan"
    assert run(stack, docs_dir, "--apply") == 2
    err = capsys.readouterr().err
    assert needs in err and "--apply --expect-docs 3 --expect-sessions 4 --expect-plan " in err
    assert run(stack, docs_dir, "--apply", "--expect-docs", "3", "--expect-sessions", "4") == 2   # counts alone: no
    assert needs in capsys.readouterr().err
    stack["demo"].add_document("late upload.md")                          # the vault changed after the dry run
    assert apply(stack, docs_dir, 3, 4) == 2
    assert "you expected 3 document and 4 session deletes, but the plan above has 4 and 4" in capsys.readouterr().err
    assert writes(stack) == []
    for option in ("--expect-docs", "--expect-plan"):                     # without --apply: a usage error
        with pytest.raises(SystemExit):
            run(stack, docs_dir, option, "4")


def test_apply_refuses_a_changed_plan_with_the_same_counts(stack: dict[str, FakeVaultApi], docs_dir: Path,
                                                          capsys: pytest.CaptureFixture[str]) -> None:
    """Counts alone accepted this: a reviewed chat deleted in the UI and a new one the rules also delete keep them at
    3 and 4, and --apply then deleted a session nobody reviewed."""
    reviewed = reviewed_plan(stack, docs_dir)
    demo = stack["demo"]
    del demo.sessions[next(s["id"] for s in demo.sessions.values() if s["title"] == "UI test")]
    notes = next(d for d in demo.docs.values() if d["filename"] == CALL_NOTES)
    fresh = demo.add_session("Fresh rehearsal", None, [cite(notes)])
    assert apply(stack, docs_dir, 3, 4, plan=reviewed) == 2
    err = capsys.readouterr().err
    assert f"you expected plan {reviewed}, but the plan above is " in err and "the items are not" in err
    assert fresh["id"] in demo.sessions and writes(stack) == []
    assert apply(stack, docs_dir, 3, 4) == 0                                 # reviewed again: that plan runs
    assert fresh["id"] not in demo.sessions


def test_the_plan_id_covers_the_failing_backups_backups_1_deletes(docs_dir: Path,
                                                                  capsys: pytest.CaptureFixture[str]) -> None:
    demo = FakeVaultApi()
    corpus = seeded(demo)
    wrong = cite(corpus[BOARD_UPDATE], content="It is $750 [S1].")
    first = demo.add_session("Backup: stipend", None, [wrong])
    stack = {"demo": demo, "other": FakeVaultApi(), "eval": FakeVaultApi()}
    reviewed = reviewed_plan(stack, docs_dir)
    del demo.sessions[first["id"]]
    redo = demo.add_session("Backup: stipend (redo)", None, [wrong])         # still 0 and 0 deletes
    assert apply(stack, docs_dir, 0, 0, "--create-backups", plan=reviewed) == 2
    assert "the counts are the same, the items are not" in capsys.readouterr().err
    assert redo["id"] in demo.sessions and writes(stack) == []


def test_apply_deletes_verifies_and_replans(stack: dict[str, FakeVaultApi], docs_dir: Path,
                                            capsys: pytest.CaptureFixture[str]) -> None:
    demo, eval_user = stack["demo"], stack["eval"]
    assert apply(stack, docs_dir, 3, 4) == 0
    out = capsys.readouterr().out
    assert {d["filename"] for d in demo.docs.values()} == {*SEED_FILES, NIST, USER_DOC}
    assert titles(demo) == {"PTO rehearsal", "Backup: PTO"}
    assert "stray.txt" not in {d["filename"] for d in eval_user.docs.values()} and len(eval_user.sessions) == 1
    deletes = [c.split("/")[3] for c in demo.calls if c.startswith("DELETE")]
    assert deletes == ["chat"] * 4 + ["documents"] * 2                     # sessions first, then documents
    assert not any(c.startswith("POST") for api in stack.values() for c in api.calls)
    assert "PASS    7/7 deleted ids now return 404" in out
    assert "nothing left to delete" in out and "Backup sessions (demo): 1 of 3" in out
    assert "make doctor" in out and "make snapshot-demo" in out and "git tag demo-v1" in out
    assert run(stack, docs_dir, "--check") == 0                               # the snapshot may go ahead now


def test_a_partial_failure_converges_on_rerun(docs_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    demo = FakeVaultApi()
    seeded(demo)
    secret = demo.add_document(SECRET)
    chat = demo.add_session("All documents", None, [answer([], [secret])])   # retrieved only
    stack = {"demo": demo, "other": FakeVaultApi(), "eval": FakeVaultApi()}
    server = users_transport({KEYS[name]: api for name, api in stack.items()})

    def flaky(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE" and chat["id"] in request.url.path:
            return httpx.Response(503, json={"error": {"code": "UNAVAILABLE", "message": "boom"}})
        return server.handle_request(request)

    main = load_script("pre_demo").main
    args = ["--api", "http://vault.test", "--docs-dir", str(docs_dir), "--apply"]
    assert main([*args, "--expect-docs", "1", "--expect-sessions", "1", "--expect-plan", reviewed_plan(stack, docs_dir)],
                transport=httpx.MockTransport(flaky)) == 1
    assert secret["id"] not in demo.docs and chat["id"] in demo.sessions
    assert "(retrieved; deleted earlier)" in capsys.readouterr().out          # what the rerun will see
    assert main([*args, "--expect-docs", "0", "--expect-sessions", "1", "--expect-plan",
                 reviewed_plan(stack, docs_dir)], transport=server) == 0
    assert chat["id"] not in demo.sessions


def golden_answers(api: FakeVaultApi) -> None:
    """Answers that pass the Backup topics' golden cases, citing the files those cases require."""
    by_name = {d["filename"]: d for d in api.docs.values()}
    replies = {"PTO": (PTO_ANSWER, [HANDBOOK]),
               "stipend": ("It was $500 a year [S1] and is $750 since October 1, 2026 [S2].", [HANDBOOK, BOARD_UPDATE]),
               "vendor": ("BrightPath: $600,000 over three years [S1]; CoreLink: $665,000 [S2]. CoreLink's SLA is "
                          "stronger: 99.9% [S2].", [BRIGHTPATH, CORELINK])}

    def answer_fn(question: str, prior: list[str], ids: list[str]) -> dict[str, Any]:
        content, files = next((r for key, r in replies.items() if key in question), ("", []))
        return {"content": content, "citations": [{"ref": f"S{i}", "document_id": by_name[f]["id"], "filename": f}
                                                  for i, f in enumerate(files, start=1)]}
    api.answer_fn = answer_fn


def test_apply_create_backups_asks_the_golden_questions(stack: dict[str, FakeVaultApi], docs_dir: Path,
                                                        capsys: pytest.CaptureFixture[str],
                                                        cases: dict[str, golden.Case]) -> None:
    demo = stack["demo"]
    golden_answers(demo)
    assert apply(stack, docs_dir, 3, 4, "--create-backups") == 0
    out = capsys.readouterr().out
    backups = [s for s in demo.sessions.values() if s["title"].startswith("Backup")]
    assert sorted(s["title"] for s in backups) == ["Backup: PTO", "Backup: proposals", "Backup: stipend"]
    assert all(s["all_documents"] for s in backups)
    asked = [q for s in backups for q in demo.questions[s["id"]]]
    assert sorted(asked) == sorted(cases[t.case_id].question for t in BACKUP_TOPICS[1:])   # PTO existed already
    assert "passes m-stipend-conflict" in out and "passes m-proposals-cost-sla" in out
    assert "Backup sessions (demo): 3 of 3 pass their golden case" in out
    posts = sum(c.startswith("POST") for c in demo.calls)
    assert apply(stack, docs_dir, 0, 0, "--create-backups") == 0           # second run: nothing to ask
    assert sum(c.startswith("POST") for c in demo.calls) == posts


def test_a_backup_answer_that_fails_its_golden_case_is_not_kept(stack: dict[str, FakeVaultApi], docs_dir: Path,
                                                                capsys: pytest.CaptureFixture[str]) -> None:
    demo = stack["demo"]
    golden_answers(demo)
    passing = demo.answer_fn
    demo.answer_fn = lambda q, prior, ids: ({**passing(q, prior, ids), "content": "It is $750 [S1]."}
                                            if "stipend" in q else passing(q, prior, ids))   # cited, but incomplete
    assert apply(stack, docs_dir, 3, 4, "--create-backups") == 1
    out = capsys.readouterr().out
    assert "FAIL    stipend" in out and "fails m-stipend-conflict: all_of" in out
    assert "Backup: stipend" not in titles(demo) and "Backup: proposals" in titles(demo)   # ours: removed again
    demo.answer_fn = lambda q, prior, ids: {"content": "I couldn't find this in your documents.",
                                            "answer_type": "not_found"}
    assert apply(stack, docs_dir, 0, 0, "--create-backups") == 1
    assert "not_found, cites nothing; no cited answer stored" in capsys.readouterr().out


def test_a_stored_backup_that_fails_is_replaced_and_reruns_converge(docs_dir: Path,
                                                                    capsys: pytest.CaptureFixture[str]) -> None:
    demo = FakeVaultApi()
    corpus = seeded(demo)
    wrong = cite(corpus[BOARD_UPDATE], content="It is $750 [S1].")          # cited, fails m-stipend-conflict
    demo.add_session("Backup: stipend", None, [{"role": "user", "content": "x"}, wrong])
    demo.add_session("Backup: PTO", None, [cite(corpus[HANDBOOK], content=PTO_ANSWER)])
    demo.add_session("Backup: PTO (old)", None, [cite(corpus[HANDBOOK], content="18 days [S1]")])
    stack = {"demo": demo, "other": FakeVaultApi(), "eval": FakeVaultApi()}
    assert run(stack, docs_dir) == 0
    out = capsys.readouterr().out
    assert "fails m-stipend-conflict: all_of, gold_citation; BACKUPS=1 deletes it and asks again" in out
    assert "fails f-pto-days: all_of; BACKUPS=1 deletes it\n" in out          # a passing PTO Backup exists
    demo.answer_fn = lambda q, prior, ids: {**wrong, "content": "Still $750 [S1]."}   # the model is off today
    assert apply(stack, docs_dir, 0, 0, "--create-backups") == 1
    assert titles(demo) == {"Backup: PTO"}                                   # nothing wrong is kept
    golden_answers(demo)
    assert apply(stack, docs_dir, 0, 0, "--create-backups") == 0
    assert "Backup sessions (demo): 3 of 3 pass their golden case" in capsys.readouterr().out
    assert sorted(titles(demo)) == ["Backup: PTO", "Backup: proposals", "Backup: stipend"]
    posts = sum(c.startswith("POST") for c in demo.calls)
    assert apply(stack, docs_dir, 0, 0, "--create-backups") == 0           # converged: nothing to ask
    assert sum(c.startswith("POST") for c in demo.calls) == posts


def test_a_backup_covering_several_topics_passes_only_if_it_passes_all(docs_dir: Path,
                                                                      capsys: pytest.CaptureFixture[str],
                                                                      cases: dict[str, golden.Case]) -> None:
    """BACKUPS=1 deletes a failing session whole: when it also held the passing PTO answer, PTO must be asked again,
    not reported as covered by a session that is gone."""
    demo = FakeVaultApi()
    corpus = seeded(demo)
    golden_answers(demo)
    both = demo.add_session("Backup answers", None, [
        {"role": "user", "content": cases["f-pto-days"].question}, cite(corpus[HANDBOOK], content=PTO_ANSWER),
        {"role": "user", "content": cases["m-stipend-conflict"].question},
        cite(corpus[BOARD_UPDATE], content="It is $750 [S1].")])
    stack = {"demo": demo, "other": FakeVaultApi(), "eval": FakeVaultApi()}
    assert run(stack, docs_dir) == 0
    out = capsys.readouterr().out
    assert "Backup sessions (demo): 0 of 3 pass their golden case" in out
    assert ("passes f-pto-days, but fails m-stipend-conflict: all_of, gold_citation; BACKUPS=1 deletes it and asks "
            "again") in out
    assert apply(stack, docs_dir, 0, 0, "--create-backups") == 0
    assert "Backup sessions (demo): 3 of 3 pass their golden case" in capsys.readouterr().out
    assert sorted(titles(demo)) == ["Backup: PTO", "Backup: proposals", "Backup: stipend"]       # PTO asked again
    assert [c for c in demo.calls if c.startswith("DELETE")] == [f"DELETE /api/v1/chat/sessions/{both['id']}"]


def test_backups_1_fails_while_a_topic_has_no_passing_session(stack: dict[str, FakeVaultApi], docs_dir: Path,
                                                              capsys: pytest.CaptureFixture[str],
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    """The last line of defence: whatever went wrong on the way, BACKUPS=1 ending without three passing Backup
    sessions is not a success."""
    monkeypatch.setattr(load_script("pre_demo"), "create_backups", lambda client, statuses, cases: [])
    assert apply(stack, docs_dir, 3, 4, "--create-backups") == 1
    assert ("incomplete: 0 failed step(s), 0 id(s) still there, 0 item(s) left, Backup not passing: stipend, "
            "proposals") in capsys.readouterr().out


def test_apply_deletes_only_the_failing_backups_the_plan_id_covers(stack: dict[str, FakeVaultApi], docs_dir: Path,
                                                                   capsys: pytest.CaptureFixture[str]) -> None:
    """A Backup session that fails after the dry run (here: one created while --apply runs) was never reviewed, so
    BACKUPS=1 leaves it alone and fails instead of deleting it."""
    demo = stack["demo"]
    golden_answers(demo)
    reviewed = reviewed_plan(stack, docs_dir)
    server = users_transport({KEYS[name]: api for name, api in stack.items()})
    handbook = next(d for d in demo.docs.values() if d["filename"] == HANDBOOK)
    late: list[dict[str, Any]] = []

    def meanwhile(request: httpx.Request) -> httpx.Response:
        response = server.handle_request(request)
        if request.method == "DELETE" and not late:
            late.append(demo.add_session("Backup: proposals", None, [cite(handbook, content="No idea [S1].")]))
        return response

    args = ["--api", "http://vault.test", "--docs-dir", str(docs_dir), "--apply", "--expect-docs", "3",
            "--expect-sessions", "4", "--expect-plan", reviewed, "--create-backups"]
    assert load_script("pre_demo").main(args, transport=httpx.MockTransport(meanwhile)) == 1
    out = capsys.readouterr().out
    assert f"skipped: failing Backup session(s) {late[0]['id'][:8]} are not in the reviewed plan" in out
    assert late[0]["id"] in demo.sessions and not any(c.startswith("POST") for c in demo.calls)


def test_a_failed_delete_is_reported_and_backups_wait(stack: dict[str, FakeVaultApi], docs_dir: Path,
                                                      capsys: pytest.CaptureFixture[str]) -> None:
    demo = stack["demo"]
    secret = next(d for d in demo.docs.values() if d["filename"] == SECRET)
    server = users_transport({KEYS[name]: api for name, api in stack.items()})

    def failing_delete(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE" and secret["id"] in request.url.path:
            return httpx.Response(500, json={"error": {"code": "INTERNAL_ERROR", "message": "boom"}})
        return server.handle_request(request)

    main = load_script("pre_demo").main
    args = ["--api", "http://vault.test", "--docs-dir", str(docs_dir), "--apply", "--expect-docs", "3",
            "--expect-sessions", "4", "--expect-plan", reviewed_plan(stack, docs_dir), "--create-backups"]
    assert main(args, transport=httpx.MockTransport(failing_delete)) == 1
    out = capsys.readouterr().out
    assert f"FAIL    demo: document {SECRET}" in out and "still to delete:" in out
    assert "skipped: the cleanup is not complete" in out          # an all-documents question could still cite it
    assert secret["id"] in demo.docs and "Old notes" not in titles(demo)
    assert not any(c.startswith("POST") for c in demo.calls)
    assert "incomplete: 1 failed step(s), 1 id(s) still there, 1 item(s) left" in out


def test_every_page_of_a_vault_is_judged(docs_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The client pages with limit=100: a leak in the oldest session of a big eval vault must still be found."""
    demo, eval_user = FakeVaultApi(), FakeVaultApi()
    seeded(demo)
    eval_corpus = seeded(eval_user)
    gone = {"id": str(uuid.uuid4()), "filename": SECRET}
    eval_user.add_session("eval run 0", None, [cite(gone)])                    # the oldest: on the last page
    for n in range(1, 130):
        eval_user.add_session(f"eval run {n}", None, [cite(eval_corpus[HANDBOOK])])
    assert run({"demo": demo, "other": FakeVaultApi(), "eval": eval_user}, docs_dir, "--check") == 1
    out = capsys.readouterr().out
    assert "eval: delete 1 of 130 sessions" in out and "eval run 0" in out


# --- the shared client --------------------------------------------------------------------------------------------
def test_messages_follow_the_cursor_back_to_the_first_page() -> None:
    from evals.api_client import VaultClient

    pages = {None: {"items": [{"seq": 3}, {"seq": 4}], "next_cursor": "c1"},
             "c1": {"items": [{"seq": 1}, {"seq": 2}], "next_cursor": None}}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pages[request.url.params.get("cursor")])

    with VaultClient("http://vault.test", "k", transport=httpx.MockTransport(handler)) as client:
        assert [m["seq"] for m in client.messages("s1")] == [1, 2, 3, 4]


def test_the_fake_api_pages_like_the_real_one() -> None:
    from evals.api_client import ApiError, VaultClient

    api = FakeVaultApi()
    docs = [api.add_document(f"doc-{n}.txt") for n in range(5)]
    chats = [api.add_session(f"chat {n}") for n in range(5)]
    api.add_session("long", None, [{"role": "user", "content": f"q{n}"} for n in range(5)])
    with VaultClient("http://vault.test", "k", transport=users_transport({"k": api})) as client:
        assert [d["id"] for d in client.list_documents(page_size=2)] == [d["id"] for d in reversed(docs)]
        assert [s["title"] for s in client.list_sessions(page_size=2)][1:] == [s["title"] for s in reversed(chats)]
        long_id = next(s["id"] for s in api.sessions.values() if s["title"] == "long")
        assert [m["content"] for m in client.messages(long_id, page_size=2)] == [f"q{n}" for n in range(5)]
        newest = client.get_json(f"/api/v1/chat/sessions/{long_id}/messages", {"limit": 2})
        assert [m["seq"] for m in newest["items"]] == [4, 5] and newest["next_cursor"]
        with pytest.raises(ApiError, match="422"):
            client.list_documents(page_size=101)
    assert sum(c == "GET /api/v1/documents" for c in api.calls) == 3 + 1       # 3 pages, then the 422
