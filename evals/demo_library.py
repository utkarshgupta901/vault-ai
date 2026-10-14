"""What a seeded user's vault may hold before the demo snapshot, and what has to go.

Pure functions of API responses (GET /documents, /chat/sessions, /chat/sessions/{id}/messages) and of the corpus
files on disk, shared by scripts/pre_demo.py (prints and applies the cleanup) and scripts/doctor.py (fails while the
demo library is not clean), so the two can never disagree about what "clean" means.

The rules:
  - A document is KNOWN when its sha256 OR its filename matches a file in demo_docs/ (seed corpus, live uploads,
    drill file, anything else there), the approved public document (evals/corpus.PUBLIC_DOCS, pinned by hash, so it
    is known even where demo_docs/user/ is missing or empty) or a file in demo_docs/user/. A filename match with
    other bytes is kept with a warning: a regenerated PDF gets a new hash, and that is no reason to delete a corpus
    document. A file that is in demo_docs/ but not named in evals/corpus.py ("extra") is kept, but blocks a
    snapshot: anyone can drop a file there, so its presence proves nothing.
  - Demo user: delete every unknown document, and the live uploads (call notes, receipt), which the demo uploads on
    stage: a pre-loaded copy would turn that upload into `duplicate: true`. Keep the seed corpus and user documents.
  - Other seeded users (other, eval): delete unknown documents only.
  - Sessions (every user): delete a session whose scope includes a document being deleted, or whose answers cited
    one; that also used (scope, citation or retrieval) a non-corpus document, being deleted now or deleted earlier
    (a delete redacts cited snippets but keeps the filename in citations and retrieval, and the answer text keeps
    what it said); that MENTIONS a deny term anywhere a viewer can read (title, messages, rewritten query,
    suggested follow-ups): the terms of the gitignored demo_docs/user/.deny_terms plus the names of those non-corpus
    documents (a name of two or more words not made of a corpus filename's words: a lone "Budget" would hit
    rehearsals about the corpus), which catches a question about a document that never retrieved it; whose scope is
    empty (every document in it was deleted); or whose title looks like a test. Keep everything else: the Backup and
    rehearsal sessions.
    One exception, on purpose: a session whose retrieval merely returned a live upload (call notes) that it did not
    cite is kept. That file is synthetic corpus text, so nothing leaks, and all-documents questions rehearsed after
    the live upload often retrieve it: deleting those would thin out /metrics/chat (computed from stored answers),
    and the snapshot needs those rehearsal numbers."""

import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evals import scoring
from evals.corpus import (
    DEMO_DOCS,
    LIVE_UPLOADS,
    PUBLIC_DOCS,
    SEED_FILES,
    STRESS_MANUAL,
    SUPPORTED_SUFFIXES,
    user_files,
)
from evals.golden import Case

DEMO_USER = "demo"
DEFAULT_TEST_TITLES = ("smoke test", "ui test")   # anywhere in the title, any case; the first match is the reason
BARE_TEST_TITLES = ("test", "tests")              # only as the WHOLE title: "the stress test manual" is a rehearsal
ROLE_LABELS = {"seed": "seed corpus", "live": "live upload", "drill": "crash-drill file",
               "public": "approved public document", "user": "real document (demo_docs/user)",
               "extra": "other demo_docs file"}
UNKNOWN_REASON = "not a demo corpus file"
LIVE_REASON = "live upload: the demo uploads it on stage"
EXTRA_REASON = "in demo_docs/ but not named in evals/corpus.py: blocks the snapshot"
DENY_TERMS_FILE = ".deny_terms"   # in demo_docs/user/: gitignored, so the terms never reach the repo
MIN_TERM_CHARS = 4                # a shorter term ("ai", "q3") would match nearly every chat

Doc = dict[str, Any]


# --- known files (the only part that reads the disk) ---------------------------------------------------------------
@dataclass(frozen=True)
class KnownFile:
    name: str
    sha256: str | None        # None: the file is not on disk here, so it can only be matched by name
    role: str                 # seed | live | drill | public | user | extra
    in_user_dir: bool = False  # read from demo_docs/user/: listed in the plan header, so that trust is visible


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def known_files(docs_dir: Path = DEMO_DOCS) -> list[KnownFile]:
    """Every corpus file with its sha256: the named corpus files (even when not generated yet), any other supported
    file in docs_dir, the approved public document, and the user's real documents in docs_dir/user/."""
    roles = {**dict.fromkeys(SEED_FILES, "seed"), **dict.fromkeys(LIVE_UPLOADS, "live"), STRESS_MANUAL: "drill"}
    extras = sorted(p.name for p in docs_dir.iterdir() if p.is_file() and p.name not in roles
                    and p.name not in PUBLIC_DOCS and p.suffix.lower() in SUPPORTED_SUFFIXES) \
        if docs_dir.is_dir() else []
    roles.update(dict.fromkeys(extras, "extra"))
    known = [KnownFile(name, sha256_file(docs_dir / name) if (docs_dir / name).is_file() else None, role)
             for name, role in roles.items()]
    user = {p: sha256_file(p) for p in user_files(docs_dir)}
    public_here = set(user.values()) & set(PUBLIC_DOCS.values())
    known += [KnownFile(name, digest, "public", digest in public_here) for name, digest in PUBLIC_DOCS.items()]
    return known + [KnownFile(p.name, digest, "user", True) for p, digest in user.items() if digest not in public_here]


def user_docs_warning(docs_dir: Path) -> str | None:
    """demo_docs/user/ is gitignored, so a fresh clone or a git worktree has none (or an empty one): a real document
    seeded from the main checkout's folder would then look unknown, and be planned for deletion. Only the approved
    public document is known without it (pinned in evals/corpus.PUBLIC_DOCS)."""
    if user_files(docs_dir):
        return None
    state = "is empty" if (docs_dir / "user").is_dir() else "does not exist here"
    return (f"{docs_dir / 'user'} {state}, so a real document seeded from it counts as unknown "
            f"(only {', '.join(PUBLIC_DOCS)} is known without it)")


# --- deny terms -----------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class DenyTerm:
    text: str                 # folded (see fold)
    label: str                # how a reason names it: a document name is shown, a .deny_terms line is not
    from_file: bool


def fold(text: str) -> str:
    """Any case, and an underscore or a run of spaces counts as one space, so a filename's "Project_Notes" matches
    "project notes" typed in a chat."""
    return re.sub(r"[\s_]+", " ", text.casefold()).strip()


def mentions(folded_text: str, term: str) -> bool:
    """The term as whole words: "atlas" matches "Atlas plan" but not "atlases"."""
    return re.search(rf"(?<!\w){re.escape(term)}(?!\w)", folded_text) is not None


def load_deny_terms(docs_dir: Path = DEMO_DOCS) -> tuple[list[DenyTerm], list[str]]:
    """Terms that no snapshot chat may mention, one per line in demo_docs/user/.deny_terms ('#' starts a comment
    line). That folder is gitignored and there is no default list on purpose: the terms name what must not leak, so
    committing them would leak them. Returns (terms, warnings about ignored lines)."""
    path = docs_dir / "user" / DENY_TERMS_FILE
    if not path.is_file():
        return [], []
    terms, warnings = [], []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        term = fold(line)
        if not term or term.startswith("#"):
            continue
        if len(term) < MIN_TERM_CHARS:
            warnings.append(f"{DENY_TERMS_FILE} line {n} ignored: under {MIN_TERM_CHARS} characters, it would match "
                            "nearly every chat")
            continue
        terms.append(DenyTerm(term, f"deny term (line {n} of {DENY_TERMS_FILE})", True))
    return terms, warnings


def words(folded_text: str) -> set[str]:
    """The runs of letters and digits: "q3 2026 board update" -> {q3, 2026, board, update}."""
    return set(re.findall(r"[^\W_]+", folded_text))


def filename_terms(names: Iterable[str], known: list[KnownFile]) -> tuple[list[DenyTerm], list[str]]:
    """A deny term per non-corpus document name (its stem: a chat says "the Unrelated Notes doc", not ".md"). The term
    is checked in every vault, so a name that would hit rehearsal chats about the corpus is skipped with a warning
    that says how to cover it instead: one that is too short, a single word ("Budget": a board-update rehearsal asks
    about the budget), or made only of one corpus filename's words ("notes" of Vendor_Call_Notes, or "Q3 Board
    Update", a renamed copy of Q3_2026_Board_Update deleted earlier)."""
    corpus = [words(fold(Path(k.name).stem)) for k in known]
    terms: dict[str, DenyTerm] = {}
    warnings = []
    for name in names:
        stem = fold(Path(name).stem)
        if stem in terms:
            continue
        parts = words(stem)
        if len(stem) < MIN_TERM_CHARS or len(parts) < 2 or any(parts <= c for c in corpus):
            warnings.append(f"{name}: its name is too short, a single word or made of a corpus filename's words, so "
                            "a chat that only mentions it is not caught; add a term for it to "
                            f"demo_docs/user/{DENY_TERMS_FILE}")
            continue
        terms[stem] = DenyTerm(stem, f'"{Path(name).stem}"', False)
    return list(terms.values()), list(dict.fromkeys(warnings))


def readable_text(session: dict[str, Any], messages: list[dict[str, Any]]) -> str:
    """Everything a viewer of the session can read, folded: its title, every message, the rewritten (standalone)
    query and the suggested follow-ups."""
    parts = [session.get("title") or ""]
    for m in messages:
        parts += [m.get("content") or "", m.get("standalone_query") or "", *(m.get("followups") or [])]
    return fold(" | ".join(parts))


# --- documents ------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class DocDecision:
    user: str
    id: str
    filename: str
    delete: bool
    reason: str
    role: str | None          # the matching corpus file's role; None = unknown document


def match_known(doc: Doc, known: list[KnownFile]) -> tuple[KnownFile | None, bool]:
    """(the corpus file this document is, True when only its filename matched). Bytes win over names, so a corpus
    file uploaded under another name is still recognised."""
    for k in known:
        if k.sha256 and k.sha256 == doc.get("sha256"):
            return k, False
    return next(((k, True) for k in known if k.name == doc.get("filename")), (None, False))


def plan_documents(user: str, docs: list[Doc], known: list[KnownFile], *,
                   demo: bool) -> tuple[list[DocDecision], list[str]]:
    """One decision per document, plus warnings about documents that are kept but look off."""
    decisions, warnings = [], []
    for doc in docs:
        k, by_name_only = match_known(doc, known)
        name = doc["filename"]
        if k is None:
            decisions.append(DocDecision(user, doc["id"], name, True, UNKNOWN_REASON, None))
            continue
        if demo and k.role == "live":
            decisions.append(DocDecision(user, doc["id"], name, True, LIVE_REASON, k.role))
            continue
        decisions.append(DocDecision(user, doc["id"], name, False,
                                     EXTRA_REASON if k.role == "extra" else ROLE_LABELS[k.role], k.role))
        if by_name_only:
            warnings.append(f"{user}: {name} kept by filename, but "
                            + ("its bytes differ from demo_docs (regenerated? re-seed if answers look off)"
                               if k.sha256 else "the file is not in demo_docs here, so its bytes were not compared"))
        if demo and k.role == "drill":
            warnings.append(f"{user}: {name} kept ({ROLE_LABELS[k.role]}), but make seed does not pre-load it")
    if demo:
        kept = [d.filename for d in decisions if not d.delete]
        warnings += [f"{user}: {n} is in the library {kept.count(n)} times (different bytes): chats retrieve "
                     "from every copy" for n in dict.fromkeys(kept) if kept.count(n) > 1]
    return decisions, warnings


# --- sessions -------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class SessionDecision:
    user: str
    id: str
    title: str | None
    delete: bool
    reasons: tuple[str, ...]
    leaks: bool               # holds non-corpus content (scope, citations, answer text, a mention): blocks a snapshot


def message_refs(messages: list[dict[str, Any]]) -> dict[str, tuple[str, bool]]:
    """{document id: (filename, cited?)} for every document an answer cited or its retrieval returned."""
    refs: dict[str, tuple[str, bool]] = {}
    for m in messages:
        for c in (m.get("retrieval") or {}).get("candidates") or []:
            if c.get("document_id"):
                refs.setdefault(str(c["document_id"]), (c.get("filename") or "?", False))
        for c in m.get("citations") or []:
            if c.get("document_id"):
                refs[str(c["document_id"])] = (c.get("filename") or "?", True)
    return refs


def title_test_reason(title: str | None, phrases: Iterable[str]) -> str | None:
    """Why the title looks like a test: a phrase anywhere in it ("UI testing", "Smoke tests"), or a title that is
    nothing but "test"/"tests". A bare "test" inside a longer title is not enough: "What does the stress test manual
    say about retries?" is a rehearsal of the crash-drill document."""
    text = (title or "").strip().casefold()
    phrase = next((p for p in phrases if p.casefold() in text), None)
    if phrase:
        return f'title has "{phrase}"'
    return f'title is "{text}"' if text in BARE_TEST_TITLES else None


def plan_session(user: str, session: dict[str, Any], messages: list[dict[str, Any]], filenames: dict[str, str],
                 deleting: dict[str, DocDecision], known_names: set[str], test_titles: Iterable[str],
                 terms: Iterable[DenyTerm] = ()) -> tuple[SessionDecision, list[str]]:
    """filenames: {id: filename} of the user's documents as listed now; deleting: {id: decision} of the documents
    this plan deletes; terms: what no chat may mention."""
    scope = [str(d) for d in session.get("document_ids") or []]
    refs = message_refs(messages)
    reasons, warnings, leaks = [], [], False
    for doc_id in dict.fromkeys(scope + list(refs)):                 # each document once, scope first
        ref_name, cited = refs.get(doc_id, ("?", False))
        name = filenames.get(doc_id, ref_name)
        uses = ", ".join(u for u, on in (("scope", doc_id in scope), ("cited", cited),
                                         ("retrieved", doc_id in refs and not cited)) if on)
        doomed, gone = deleting.get(doc_id), doc_id not in filenames
        corpus = doomed.role is not None if doomed else name in known_names
        if doomed and (not corpus or doc_id in scope or cited):     # a corpus file only retrieved: kept on purpose
            reasons.append(f"{name} ({uses}; deleting now)")
            leaks = leaks or not corpus
        elif gone and not corpus:
            reasons.append(f"{name} ({uses}; deleted earlier)")
            leaks = True
        elif gone and cited:
            warnings.append(f"{user}: session \"{session.get('title') or '(untitled)'}\" cites an earlier copy of "
                            f"{name}: its citations show 'source deleted'")
    text = readable_text(session, messages)
    hits = [t.label for t in terms if mentions(text, t.text)]
    if hits:
        reasons.append(f"mentions {', '.join(hits)}")
        leaks = True
    if not session.get("all_documents") and not scope:
        reasons.append("empty scope: its documents were deleted")
    title_reason = title_test_reason(session.get("title"), test_titles)
    if title_reason:
        reasons.append(title_reason)
    decision = SessionDecision(user, session["id"], session.get("title"), bool(reasons), tuple(reasons), leaks)
    return decision, [] if decision.delete else warnings


# --- the whole plan -------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class UserLibrary:
    """One user's vault as the API returned it."""

    name: str
    documents: list[Doc]
    sessions: list[dict[str, Any]]
    messages: dict[str, list[dict[str, Any]]] = field(default_factory=dict)   # session id -> history


@dataclass
class Plan:
    documents: list[DocDecision] = field(default_factory=list)
    sessions: list[SessionDecision] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    terms: list[DenyTerm] = field(default_factory=list)          # what the session text was scanned for

    @property
    def doc_deletes(self) -> list[DocDecision]:
        return [d for d in self.documents if d.delete]

    @property
    def session_deletes(self) -> list[SessionDecision]:
        return [s for s in self.sessions if s.delete]

    @property
    def untrusted(self) -> list[DocDecision]:
        """Kept only because a file of that name or bytes sits in demo_docs/ without being a named corpus file."""
        return [d for d in self.documents if not d.delete and d.role == "extra"]

    @property
    def empty(self) -> bool:
        return not self.doc_deletes and not self.session_deletes

    @property
    def blocks_snapshot(self) -> bool:
        """Non-corpus content is still in the database: a document to delete, an untrusted document, or a session
        that uses or mentions one. A session deleted only for its title (e.g. "UI test" over the corpus) is untidy,
        not a leak."""
        return bool(self.doc_deletes) or bool(self.untrusted) or any(s.leaks for s in self.session_deletes)


def gone_names(lib: UserLibrary, known_names: set[str]) -> list[str]:
    """Non-corpus documents deleted earlier that this vault's answers still name (citations and retrieval keep the
    filename after a delete)."""
    current = {d["id"] for d in lib.documents}
    return [name for messages in lib.messages.values() for doc_id, (name, _) in message_refs(messages).items()
            if doc_id not in current and name not in known_names and name != "?"]


def plan_cleanup(libraries: Iterable[UserLibrary], known: list[KnownFile], *,
                 test_titles: Iterable[str] = DEFAULT_TEST_TITLES, deny_terms: Iterable[DenyTerm] = (),
                 demo_user: str = DEMO_USER) -> Plan:
    """Documents first (every vault), then sessions, so a session in any vault is checked for the names of every
    non-corpus document that is going now or went earlier, in any vault."""
    plan = Plan()
    libraries = list(libraries)
    known_names = {k.name for k in known}
    test_titles = tuple(test_titles)
    deleting: dict[str, dict[str, DocDecision]] = {}
    for lib in libraries:
        docs, warnings = plan_documents(lib.name, lib.documents, known, demo=lib.name == demo_user)
        deleting[lib.name] = {d.id: d for d in docs if d.delete}
        plan.documents += docs
        plan.warnings += warnings
    names = [d.filename for d in plan.doc_deletes if d.role is None]
    names += [name for lib in libraries for name in gone_names(lib, known_names)]
    stems, stem_warnings = filename_terms(names, known)
    plan.terms = [*deny_terms, *stems]
    plan.warnings += stem_warnings
    for lib in libraries:
        filenames = {d["id"]: d["filename"] for d in lib.documents}
        for session in lib.sessions:
            decision, session_warnings = plan_session(lib.name, session, lib.messages.get(session["id"], []),
                                                      filenames, deleting[lib.name], known_names, test_titles,
                                                      plan.terms)
            plan.sessions.append(decision)
            plan.warnings += session_warnings
    return plan


# --- Backup sessions (the "OpenAI is down" fallback in the demo checklist) ------------------------------------------
@dataclass(frozen=True)
class BackupTopic:
    topic: str                # shown in the title: "Backup: PTO"
    keyword: str              # a Backup session whose title has a word starting with this covers the topic
    case_id: str              # the demo-tagged case in evals/golden.yaml whose question it asks


BACKUP_TOPICS = (BackupTopic("PTO", "pto", "f-pto-days"),
                 BackupTopic("stipend", "stipend", "m-stipend-conflict"),
                 BackupTopic("proposals", "proposal", "m-proposals-cost-sla"))


@dataclass(frozen=True)
class BackupCandidate:
    session: dict[str, Any]
    passes: bool              # every answer it shows passes the topic's golden case, and it fails no other topic
    detail: str               # "passes f-pto-days", "fails f-pto-days: all_of", "no cited answer stored"


@dataclass(frozen=True)
class BackupStatus:
    topic: BackupTopic
    covering: tuple[BackupCandidate, ...]    # the Backup sessions that cover the topic, passing ones first

    @property
    def best(self) -> BackupCandidate | None:
        return self.covering[0] if self.covering else None

    @property
    def session(self) -> dict[str, Any] | None:
        return self.best.session if self.best else None

    @property
    def answered(self) -> bool:
        return bool(self.best and self.best.passes)

    @property
    def failing(self) -> list[BackupCandidate]:
        """Backup sessions of this topic that do not pass (here or for another topic they cover): on stage a wrong
        Backup is worse than none, so BACKUPS=1 deletes them (and asks again when no passing one is left)."""
        return [c for c in self.covering if not c.passes]


def is_backup(title: str | None) -> bool:
    return (title or "").strip().casefold().startswith("backup")


def has_cited_answer(messages: list[dict[str, Any]]) -> bool:
    return any(m.get("role") == "assistant" and m.get("status") == "complete" and m.get("answer_type") == "answered"
               and any(not c.get("source_deleted") for c in m.get("citations") or []) for m in messages)


def same_question(text: str | None, question: str) -> bool:
    return (text or "").strip().casefold() == question.strip().casefold()


def asks(messages: list[dict[str, Any]], question: str) -> bool:
    return any(m.get("role") == "user" and same_question(m.get("content"), question) for m in messages)


def replies_to(messages: list[dict[str, Any]], question: str) -> list[dict[str, Any]]:
    """The assistant messages that answer `question`, oldest first. An answer's question is the one its reply_to_id
    names (a retry appends a second reply to an earlier question), else the user message just before it."""
    users = {str(m["id"]): m for m in messages if m.get("role") == "user" and m.get("id")}
    replies, previous = [], None
    for m in messages:
        if m.get("role") == "user":
            previous = m
        elif m.get("role") == "assistant":
            asked = users.get(str(m["reply_to_id"])) if m.get("reply_to_id") else previous
            if asked is not None and same_question(asked.get("content"), question):
                replies.append(m)
    return replies


def backup_verdict(messages: list[dict[str, Any]], case: Case) -> tuple[bool, str]:
    """(passes, why) for a Backup session's stored answers to its topic, scored the way `make eval-demo` scores a
    live one (evals/scoring.py against the golden case): on stage the stored answer stands in for a live one, so
    being cited is not enough. EVERY reply to the golden question must pass (in a session that never asks it, every
    answer): opening the session shows them all, so a passing answer does not excuse a wrong one above or below it,
    and a passing reply to another question counts for nothing. The detail names the newest problem."""
    answers = (replies_to(messages, case.question) if asks(messages, case.question)
               else [m for m in messages if m.get("role") == "assistant"])
    if not answers:
        return False, "no cited answer stored"
    problems = []
    for m in answers:
        if not has_cited_answer([m]):
            problems.append("no cited answer stored" if len(answers) == 1 else "an answer is not cited")
            continue
        verdict = scoring.score_answer(case.expect, m)
        if not verdict["passed"]:
            problems.append(f"fails {case.id}: {', '.join(verdict['failed'])}")
    if not problems:
        return True, f"passes {case.id}"
    return False, problems[-1] + (f" ({len(problems)} of {len(answers)} answers)" if len(answers) > 1 else "")


def covers(session: dict[str, Any], history: list[dict[str, Any]], topic: BackupTopic, case: Case) -> bool:
    """The session's title names the topic, or one of its messages asks the topic's golden question."""
    return (asks(history, case.question)
            or re.search(rf"\b{re.escape(topic.keyword)}", session.get("title") or "", re.IGNORECASE) is not None)


def backup_status(sessions: list[dict[str, Any]], messages: dict[str, list[dict[str, Any]]],
                  cases: Mapping[str, Case], *, exclude: Iterable[str] = ()) -> list[BackupStatus]:
    """For each topic, the Backup sessions that cover it, each judged against the golden case. A session that covers
    several topics passes none of them unless it passes all of them: BACKUPS=1 deletes a failing session whole, so
    its passing answer goes too, and the topic must be asked again. cases: {case id: Case}; exclude: sessions being
    deleted."""
    excluded = set(exclude)
    verdicts: dict[str, dict[BackupTopic, tuple[bool, str]]] = {}     # session id -> {topic it covers: verdict}
    for s in sessions:
        if s["id"] in excluded or not is_backup(s.get("title")):
            continue
        history = messages.get(s["id"], [])
        verdicts[s["id"]] = {t: backup_verdict(history, cases[t.case_id]) for t in BACKUP_TOPICS
                             if covers(s, history, t, cases[t.case_id])}
    statuses = []
    for topic in BACKUP_TOPICS:
        covering = []
        for s in sessions:
            mine = verdicts.get(s["id"], {})
            if topic not in mine:
                continue
            passes, detail = mine[topic]
            elsewhere = [why for t, (ok, why) in mine.items() if t != topic and not ok]
            if passes and elsewhere:
                passes, detail = False, f"{detail}, but {elsewhere[0]}"
            covering.append(BackupCandidate(s, passes, detail))
        statuses.append(BackupStatus(topic, tuple(sorted(covering, key=lambda c: not c.passes))))
    return statuses
