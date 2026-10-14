"""Pre-demo cleanup (make pre-demo): leave the seeded users' vaults holding only the demo corpus, and make sure the
three Backup sessions exist and pass their golden cases, so `make snapshot-demo` freezes exactly what the demo should
show.

    uv run python scripts/pre_demo.py                       # dry run (default): print the plan and the command below
    uv run python scripts/pre_demo.py --apply --expect-docs 3 --expect-sessions 6 --expect-plan 1f0c3a9e5b72
                                        # make pre-demo APPLY=1 EXPECT_DOCS=3 EXPECT_SESSIONS=6 EXPECT_PLAN=1f0c3a9e5b72
    ... --create-backups                                    # ... BACKUPS=1: also the Backup sessions
    uv run python scripts/pre_demo.py --check               # exit 1 while non-corpus content is left

--apply executes only the plan that was reviewed: it needs the delete counts and the plan id the dry run printed (12
hex characters of sha256 over every id the run may delete, the failing Backup sessions BACKUPS=1 replaces included),
and refuses when the plan it computes now differs (an upload or a chat in between, even one that leaves the counts
equal). It also refuses while demo_docs/user/ holds no file, because a real document seeded from it would look
unknown; --no-user-docs states that there is none here.

The rules live in evals/demo_library.py (make doctor applies the same ones). Every change goes through the HTTP API
with the seeded keys, so a delete is the normal one: the S3 object, pages, chunks and insights go with the document,
and other chats' cited snippets of it are redacted. Sessions are deleted before documents, so the answers that quote
a removed file are the first thing to go.

Exit codes: 0 done (with --check: clean); 1 --check found non-corpus content, or a delete, the verification or a
Backup answer failed; 2 nothing was changed: the vaults could not be read (API down, a key rejected under --check, an
unexpected response), demo_docs/user/ holds no file (without --no-user-docs), or --apply's expected counts or plan id
do not match the plan."""

import argparse
import hashlib
import os
import shlex
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from evals import golden  # noqa: E402
from evals.api_client import API_PREFIX, ApiError, VaultClient  # noqa: E402
from evals.corpus import DEMO_DOCS, user_files  # noqa: E402
from evals.demo_library import (  # noqa: E402
    BACKUP_TOPICS,
    DEFAULT_TEST_TITLES,
    DEMO_USER,
    DENY_TERMS_FILE,
    ROLE_LABELS,
    BackupStatus,
    DenyTerm,
    DocDecision,
    KnownFile,
    Plan,
    SessionDecision,
    UserLibrary,
    backup_status,
    backup_verdict,
    is_backup,
    known_files,
    load_deny_terms,
    plan_cleanup,
    user_docs_warning,
)

DEFAULT_API = "http://localhost:8000"
DEFAULT_KEY = "vault-demo-key"
DEFAULT_OTHERS = "other=vault-other-key,eval=vault-eval-key"
LIST_KEPT_UP_TO = 10      # another user's kept sessions are summarised beyond this (each eval run leaves dozens)
NAME_WIDTH = 44
COLOURS = {"DELETE": "31", "deleted": "31", "FAIL": "31", "WARN": "33", "MISSING": "33", "keep": "32", "ok": "32",
           "PASS": "32", "asked": "36"}
NEXT_STEPS = ("make doctor           the 'demo library' check must PASS",
              "make eval-demo        demo cases must pass 3/3 on the cleaned library",
              "make snapshot-demo    refuses while scripts/pre_demo.py --check fails",
              "git tag demo-v1")
EXPECT_OPTIONS = ("--expect-docs", "--expect-sessions", "--expect-plan")   # what binds --apply to the reviewed plan


# --- output ---------------------------------------------------------------------------------------------------------
def paint(word: str, width: int = 7) -> str:
    text = word.ljust(width)
    if word not in COLOURS or not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return text
    return f"\033[{COLOURS[word]}m{text}\033[0m"


def fit(text: str | None, width: int = NAME_WIDTH) -> str:
    """A fixed-width column: long filenames and titles are cut with an ellipsis instead of breaking the table."""
    text = text or "(untitled)"
    return (text if len(text) <= width else text[:width - 1] + "…").ljust(width)


def print_documents(user: str, docs: list[DocDecision]) -> None:
    deletes = [d for d in docs if d.delete]
    print(f"\n{user}: " + (f"delete {len(deletes)} of {len(docs)} documents" if docs else "no documents"))
    for d in deletes + [d for d in docs if not d.delete]:
        word = "DELETE" if d.delete else ("WARN" if d.role == "extra" else "keep")
        print(f"  {paint(word)} {fit(d.filename)} {d.id[:8]}  {d.reason}")


def print_sessions(user: str, sessions: list[SessionDecision], *, list_kept: bool) -> None:
    deletes = [s for s in sessions if s.delete]
    kept = [s for s in sessions if not s.delete]
    print(f"{user}: " + (f"delete {len(deletes)} of {len(sessions)} sessions" if sessions else "no sessions"))
    for s in deletes:
        print(f"  {paint('DELETE')} {fit(s.title)} {s.id[:8]}  {'; '.join(s.reasons)}")
    if kept and not list_kept:
        print(f"  {paint('keep')} {len(kept)} sessions (--verbose lists them)")
        return
    for s in kept:
        print(f"  {paint('keep')} {fit(s.title)} {s.id[:8]}  {'backup' if is_backup(s.title) else ''}".rstrip())


def print_plan(plan: Plan, libraries: list[UserLibrary], *, verbose: bool) -> None:
    for lib in libraries:
        print_documents(lib.name, [d for d in plan.documents if d.user == lib.name])
        sessions = [s for s in plan.sessions if s.user == lib.name]
        print_sessions(lib.name, sessions, list_kept=verbose or lib.name == DEMO_USER
                       or sum(not s.delete for s in sessions) <= LIST_KEPT_UP_TO)


def print_backups(statuses: list[BackupStatus], questions: dict[str, str], *, hint: bool) -> None:
    ready = sum(s.answered for s in statuses)
    print(f"\nBackup sessions ({DEMO_USER}): {ready} of {len(statuses)} pass their golden case")
    for st in statuses:
        topic = st.topic.topic.ljust(10)
        if st.best is None:
            print(f"  {paint('MISSING')} {topic} would ask: {questions[st.topic.case_id]}")
        for c in st.covering:
            where = f"{fit(c.session.get('title'), 28)} {c.session['id'][:8]}"
            print(f"  {paint('ok' if c.passes else 'WARN')} {topic} {where}  {c.detail}"
                  + ("" if c.passes else "; BACKUPS=1 deletes it" + ("" if st.answered else " and asks again")))
    if hint and ready < len(statuses):
        print("  -> add BACKUPS=1 to the apply command: it replaces failing Backup sessions and asks the missing ones "
              "(new all-documents sessions titled 'Backup: <topic>'; one answer call each)")


def print_rehearsals(plan: Plan) -> None:
    kept = [s for s in plan.sessions if s.user == DEMO_USER and not s.delete and not is_backup(s.title)]
    print(f"Rehearsal sessions ({DEMO_USER}): {len(kept)} kept")
    if not kept:
        print(f"  {paint('WARN')} rehearse the demo once before the snapshot, so /metrics/chat shows real numbers")


def print_known(known: list[KnownFile], docs_dir: Path, terms: list[DenyTerm], *, no_user_docs: bool) -> None:
    """What the plan trusts, before the plan: every file that makes a document "known", and what chats may not
    mention. A file dropped into demo_docs/user/ is trusted, so it is listed by name and hash, never silently."""
    counts = Counter(k.role for k in known)
    parts = [f"{counts[role]} {label}" for role, label in ROLE_LABELS.items() if counts[role]]
    absent = [k.name for k in known if k.sha256 is None]
    print(f"Known files ({docs_dir}): {', '.join(parts)}"
          + (f"; not on disk, matched by name only: {', '.join(absent)}" if absent else ""))
    trusted = [f"{k.name} {(k.sha256 or '')[:8]}" + (f" ({ROLE_LABELS[k.role]})" if k.role == "public" else "")
               for k in known if k.in_user_dir]
    print(f"Trusted real documents ({docs_dir / 'user'}): "
          + (", ".join(trusted) or ("none (--no-user-docs)" if no_user_docs else "none")))
    extras = [f"{k.name} {(k.sha256 or '')[:8]}" for k in known if k.role == "extra"]
    if extras:
        print(f"{paint('WARN')} in demo_docs/ but not named in evals/corpus.py (a vault copy blocks the snapshot): "
              + ", ".join(extras))
    from_file = sum(t.from_file for t in terms)
    names = [t.label for t in terms if not t.from_file]       # shown: a name term is checked in every vault
    print(f"Deny terms (title, messages, rewritten query, follow-ups): {from_file} from "
          f"{docs_dir / 'user' / DENY_TERMS_FILE}" + ("" if from_file else " (no such file, or no terms in it)")
          + f", plus the names of {len(names)} non-corpus documents (deleting now or deleted earlier)"
          + (f": {', '.join(names)}" if names else ""))


def print_summary(plan: Plan) -> None:
    for warning in plan.warnings:
        print(f"{paint('WARN')} {warning}")
    print(f"\nPlan: delete {len(plan.doc_deletes)} of {len(plan.documents)} documents and "
          f"{len(plan.session_deletes)} of {len(plan.sessions)} sessions.")


def failing_backups(statuses: list[BackupStatus]) -> set[str]:
    """The ids of the Backup sessions BACKUPS=1 deletes (a session covering several topics is listed once)."""
    return {c.session["id"] for st in statuses for c in st.failing}


def plan_id(plan: Plan, backups: list[BackupStatus]) -> str:
    """What --expect-plan must match: 12 hex characters of sha256 over every id this run may delete. Equal counts are
    not the same plan (a reviewed chat deleted in the UI and a new one created keep them equal). The failing Backup
    sessions are always in it, with or without BACKUPS=1, so adding BACKUPS=1 to the printed command needs no new
    dry run."""
    items = sorted([f"document:{d.user}:{d.id}" for d in plan.doc_deletes]
                   + [f"session:{s.user}:{s.id}" for s in plan.session_deletes]
                   + [f"backup:{DEMO_USER}:{i}" for i in failing_backups(backups)])
    return hashlib.sha256("\n".join(items).encode()).hexdigest()[:12]


def apply_command(args: argparse.Namespace, argv: list[str], plan: Plan, reviewed: str) -> str:
    """The exact command that executes THIS plan: it carries the plan's delete counts and its id (see plan_id), so
    --apply refuses when the vaults changed after the dry run. The make form when the Makefile can express the
    options, else the script."""
    docs, sessions = len(plan.doc_deletes), len(plan.session_deletes)
    if args.docs_dir == DEMO_DOCS and args.others == DEFAULT_OTHERS and args.test_titles == DEFAULT_TEST_TITLES:
        options = [f"API={args.api}"] if args.api != DEFAULT_API else []
        options += [f"KEY={args.key}"] if args.key != DEFAULT_KEY else []
        options += ["NO_USER_DOCS=1"] if args.no_user_docs else []
        options += ["BACKUPS=1"] if args.create_backups else []
        return shlex.join(["make", "pre-demo", "APPLY=1", f"EXPECT_DOCS={docs}", f"EXPECT_SESSIONS={sessions}",
                           f"EXPECT_PLAN={reviewed}", *options])
    kept, skip = [], False
    for arg in argv:                                    # drop an earlier --apply and its expectations
        if skip:
            skip = False
        elif arg in EXPECT_OPTIONS:
            skip = True
        elif arg != "--apply" and not arg.startswith(tuple(f"{o}=" for o in EXPECT_OPTIONS)):
            kept.append(arg)
    return shlex.join(["uv", "run", "python", "scripts/pre_demo.py", *kept, "--apply", "--expect-docs", str(docs),
                       "--expect-sessions", str(sessions), "--expect-plan", reviewed])


def refusal(args: argparse.Namespace, plan: Plan, reviewed: str) -> str | None:
    """Why --apply must not run the plan it computed, or None. The counts make a change readable; the plan id
    catches the change that keeps them equal."""
    expected = (args.expect_docs, args.expect_sessions)
    planned = (len(plan.doc_deletes), len(plan.session_deletes))
    if None in expected or args.expect_plan is None:
        return ("--apply needs --expect-docs, --expect-sessions and --expect-plan: the delete counts and the plan id "
                "of the dry run you reviewed")
    if expected != planned:
        return (f"you expected {expected[0]} document and {expected[1]} session deletes, but the plan above has "
                f"{planned[0]} and {planned[1]}: the vaults changed since the dry run")
    if args.expect_plan.strip().casefold() != reviewed:
        return (f"you expected plan {args.expect_plan}, but the plan above is {reviewed}: the counts are the same, "
                "the items are not (a document, a chat or a failing Backup session changed since the dry run)")
    return None


# --- reading the vaults -----------------------------------------------------------------------------------------------
def parse_users(demo_key: str, others: str) -> dict[str, str]:
    """{user name: API key}, the demo user first. --others is NAME=KEY,NAME=KEY ('' for none)."""
    users = {DEMO_USER: demo_key}
    for item in filter(None, (part.strip() for part in others.split(","))):
        name, _, key = item.partition("=")
        if not name or not key:
            raise ValueError(f"--others takes NAME=KEY pairs separated by commas, got {item!r}")
        users[name] = key
    return users


def fetch_library(client: VaultClient, name: str) -> UserLibrary:
    docs = client.list_documents()
    sessions = client.list_sessions()
    messages: dict[str, list[dict[str, Any]]] = {}
    for s in sessions:
        try:
            messages[s["id"]] = client.messages(s["id"])
        except ApiError as exc:
            if exc.status != 404:          # 404: deleted while we were listing; nothing to judge
                raise
    return UserLibrary(name, docs, sessions, messages)


def gather(clients: dict[str, VaultClient]) -> tuple[list[UserLibrary], list[str]]:
    """Every user's vault. The demo user's must load (errors propagate); another user whose key is rejected (e.g. on
    a deploy that only seeds the demo user) is skipped with a warning, which --check turns into exit 2."""
    libraries, skipped = [], []
    for name, client in clients.items():
        try:
            libraries.append(fetch_library(client, name))
        except ApiError as exc:
            if name == DEMO_USER or exc.status != 401:
                raise
            skipped.append(f"{name}: API key rejected, this user was not checked")
    return libraries, skipped


def describe(exc: Exception) -> str:
    """An error for one line: API and network errors say what they are; anything else (an HTML page or a different
    service answering on the port) gets its type, since its message alone ("Expecting value") explains nothing."""
    return str(exc) if isinstance(exc, ApiError | httpx.HTTPError) else f"{type(exc).__name__}: {exc}"


def backup_cases() -> dict[str, golden.Case]:
    """The demo-tagged cases in evals/golden.yaml behind the Backup topics: a Backup session asks exactly the question
    that `make eval-demo` checks, and its stored answer must pass the same expectations. Loaded only when Backups are
    shown or asked: the snapshot gate (--check) must not depend on the golden set."""
    cases = {c.id: c for c in golden.load(starters=None).cases}
    missing = [t.case_id for t in BACKUP_TOPICS if t.case_id not in cases or not cases[t.case_id].is_demo]
    if missing:
        raise SystemExit(f"evals/golden.yaml has no demo-tagged case {missing} for the Backup sessions")
    return {t.case_id: cases[t.case_id] for t in BACKUP_TOPICS}


def demo_backups(libraries: list[UserLibrary], cases: dict[str, golden.Case],
                 plan: Plan | None = None) -> list[BackupStatus]:
    """The demo user's Backup topics; with a plan, as they will be once its deletions ran."""
    demo = next(lib for lib in libraries if lib.name == DEMO_USER)
    exclude = [s.id for s in plan.session_deletes] if plan else []
    return backup_status(demo.sessions, demo.messages, cases, exclude=exclude)


# --- changing the vaults ----------------------------------------------------------------------------------------------
def apply_plan(plan: Plan, clients: dict[str, VaultClient]) -> list[str]:
    """Sessions first, then documents. One failure does not stop the rest: a rerun plans whatever is left."""
    failures = []
    steps = ([("session", s.user, s.id, s.title, clients[s.user].delete_session) for s in plan.session_deletes]
             + [("document", d.user, d.id, d.filename, clients[d.user].delete_document) for d in plan.doc_deletes])
    for kind, user, item_id, name, delete in steps:
        try:
            gone = delete(item_id)
        except (ApiError, httpx.HTTPError) as exc:
            failures.append(f"{user}: {kind} {name} ({item_id[:8]}): {exc}")
            print(f"  {paint('FAIL')} {user}: {kind:<8} {fit(name)} {item_id[:8]}  {exc}")
            continue
        print(f"  {paint('deleted')} {user}: {kind:<8} {fit(name)} {item_id[:8]}"
              + ("" if gone else "  (was already gone)"))
    return failures


def verify_gone(plan: Plan, clients: dict[str, VaultClient]) -> list[str]:
    """Every deleted id must now be a 404 for its owner. Returns the ones that still answer."""
    targets = ([(s.user, f"{API_PREFIX}/chat/sessions/{s.id}", f"session {s.title}") for s in plan.session_deletes]
               + [(d.user, f"{API_PREFIX}/documents/{d.id}", f"document {d.filename}") for d in plan.doc_deletes])
    return [f"{user}: {label} still exists" for user, path, label in targets
            if clients[user].request("GET", path, ok=(200, 404)).status_code != 404]


def create_backups(client: VaultClient, statuses: list[BackupStatus], cases: dict[str, golden.Case]) -> list[str]:
    """First delete every failing Backup session (evals/scoring.py against each golden case it covers), each once,
    then, per topic that no surviving session answers, ask the golden question in a new all-documents session
    'Backup: <topic>'. Deletes first: one session can cover several topics, so a topic it passed must not count as
    answered once it is gone. On stage a Backup stands in for a live answer, so a new answer that fails is deleted
    again too: every rerun starts clean and converges on a passing answer instead of keeping a wrong one."""
    failures: list[str] = []
    deleted: set[str] = set()
    for st in statuses:
        for c in st.failing:
            if c.session["id"] in deleted:
                continue
            client.delete_session(c.session["id"])
            deleted.add(c.session["id"])
            print(f"  {paint('deleted')} {st.topic.topic:<10} {fit(c.session.get('title'), 28)} "
                  f"{c.session['id'][:8]}  {c.detail}")
    for st in statuses:
        if any(c.passes and c.session["id"] not in deleted for c in st.covering):
            continue
        topic = st.topic.topic
        case = cases[st.topic.case_id]
        try:
            session = client.create_session(None, title=f"Backup: {topic}")
            answer = client.ask(session["id"], case.question)["answer"]
        except (ApiError, httpx.HTTPError) as exc:
            failures.append(f"Backup: {topic}: {exc}")
            print(f"  {paint('FAIL')} {topic:<10} {exc}")
            continue
        passes, detail = backup_verdict([answer], case)
        cites = sorted({c.get("filename") or "?" for c in answer.get("citations") or []})
        print(f"  {paint('asked' if passes else 'FAIL')} {topic:<10} {session['id'][:8]}  "
              f"{answer.get('answer_type')}, cites {', '.join(cites) or 'nothing'}; {detail}")
        if not passes:
            failures.append(f"Backup: {topic}: {detail}")
            client.delete_session(session["id"])
            print(f"  {paint('deleted')} {topic:<10} {session['id'][:8]}  rerun BACKUPS=1 to ask again")
    return failures


def execute(args: argparse.Namespace, clients: dict[str, VaultClient], plan: Plan, known: list[KnownFile],
            deny_terms: list[DenyTerm], cases: dict[str, golden.Case], reviewed_backups: set[str]) -> int:
    """Delete, prove each deleted id is a 404, re-list and re-plan (must be empty), then the Backup sessions: only
    the failing ones the reviewed plan id covers (reviewed_backups) may be deleted."""
    questions = {case_id: case.question for case_id, case in cases.items()}
    print("\nApplying")
    failures = apply_plan(plan, clients)
    if plan.empty:
        print("  nothing to delete")
    print("\nVerifying")
    still = verify_gone(plan, clients)
    if not plan.empty:
        deleted = len(plan.doc_deletes) + len(plan.session_deletes)
        print(f"  {paint('FAIL' if still else 'PASS')} {deleted - len(still)}/{deleted} deleted ids now return 404"
              + (f"; still there: {', '.join(still)}" if still else ""))
    libraries, _ = gather(clients)
    replan = plan_cleanup(libraries, known, test_titles=args.test_titles, deny_terms=deny_terms)
    totals = ", ".join(f"{lib.name} {len(lib.documents)} documents / {len(lib.sessions)} sessions"
                       for lib in libraries)
    print(f"  {paint('PASS' if replan.empty else 'FAIL')} re-listed ({totals}): "
          + ("nothing left to delete" if replan.empty else "still to delete:"))
    if not replan.empty:
        print_plan(replan, libraries, verbose=False)
    statuses = demo_backups(libraries, cases)
    if args.create_backups and not all(s.answered and not s.failing for s in statuses):
        print("\nCreating Backup sessions")
        unreviewed = sorted(failing_backups(statuses) - reviewed_backups)
        if not replan.empty:     # an all-documents question now could still retrieve what is left, and be deleted
            print(f"  {paint('WARN')} skipped: the cleanup is not complete")
        elif unreviewed:         # it failed (or appeared) after the dry run: deleting it was never reviewed
            failures.append(f"Backup sessions not in the reviewed plan: {', '.join(i[:8] for i in unreviewed)}")
            print(f"  {paint('WARN')} skipped: failing Backup session(s) {', '.join(i[:8] for i in unreviewed)} "
                  "are not in the reviewed plan")
        else:
            failures += create_backups(clients[DEMO_USER], statuses, cases)
            statuses = demo_backups([fetch_library(clients[DEMO_USER], DEMO_USER)], cases)
    print_backups(statuses, questions, hint=not args.create_backups)
    print("\nNext steps")
    for n, step in enumerate(NEXT_STEPS, start=1):
        print(f"  {n}. {step}")
    # BACKUPS=1 promises three passing Backup sessions: whatever went wrong on the way, ending without them fails.
    unready = [s.topic.topic for s in statuses if not s.answered or s.failing] if args.create_backups else []
    if failures or still or not replan.empty or unready:
        print(f"\n{paint('FAIL')} incomplete: {len(failures)} failed step(s), {len(still)} id(s) still there, "
              f"{len(replan.doc_deletes) + len(replan.session_deletes)} item(s) left"
              + (f", Backup not passing: {', '.join(unready)}" if unready else "") + "; rerun make pre-demo")
        return 1
    return 0


# --- main -------------------------------------------------------------------------------------------------------------
def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Pre-demo cleanup: dry run (default), --apply or --check.")
    p.add_argument("--api", default=DEFAULT_API)
    p.add_argument("--key", default=DEFAULT_KEY, help="the demo user's API key")
    p.add_argument("--others", default=DEFAULT_OTHERS, help="other seeded users, NAME=KEY,... ('' for none)")
    p.add_argument("--docs-dir", type=Path, default=DEMO_DOCS, help="the demo_docs folder (with user/) to trust")
    p.add_argument("--no-user-docs", action="store_true",
                   help="state that this deploy has no real document besides the approved public one, so --apply "
                        "may run while demo_docs/user/ is missing or empty")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="execute the plan (default: dry run, changes nothing)")
    mode.add_argument("--check", action="store_true",
                      help="exit 1 while non-corpus content is left, 2 when a vault cannot be read "
                           "(scripts/snapshot_demo.sh runs this)")
    p.add_argument("--expect-docs", type=int, metavar="N",
                   help="with --apply (required): the documents the reviewed dry run deletes")
    p.add_argument("--expect-sessions", type=int, metavar="M",
                   help="with --apply (required): the sessions the reviewed dry run deletes")
    p.add_argument("--expect-plan", metavar="ID",
                   help="with --apply (required): the plan id the reviewed dry run printed (its apply command has it)")
    p.add_argument("--create-backups", action="store_true",
                   help="with --apply: replace failing Backup sessions, ask the missing ones (PTO, stipend, proposals)")
    p.add_argument("--test-title", dest="test_titles", action="append", metavar="PHRASE",
                   help=f"delete sessions whose title contains this phrase (repeatable; default: "
                        f"{', '.join(DEFAULT_TEST_TITLES)}; a title that is just 'test' always goes)")
    p.add_argument("--verbose", action="store_true", help="list every kept session of every user")
    return p


def main(argv: list[str] | None = None, *, transport: Any = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    p = parser()
    args = p.parse_args(argv)
    args.test_titles = tuple(args.test_titles or DEFAULT_TEST_TITLES)
    if not args.apply and any(v is not None for v in (args.expect_docs, args.expect_sessions, args.expect_plan)):
        p.error("--expect-docs / --expect-sessions / --expect-plan go with --apply")
    here = user_files(args.docs_dir)
    if args.no_user_docs and here:
        print(f"refusing: --no-user-docs, but {args.docs_dir / 'user'} holds {', '.join(f.name for f in here)}",
              file=sys.stderr)
        return 2
    no_user_docs = None if args.no_user_docs else user_docs_warning(args.docs_dir)
    if args.apply and no_user_docs:
        print(f"refusing to apply: {no_user_docs}. Run it from the main checkout or pass --docs-dir; if this deploy "
              "really has no real document besides that one, say so with --no-user-docs (make pre-demo "
              "NO_USER_DOCS=1).", file=sys.stderr)
        return 2
    try:
        users = parse_users(args.key, args.others)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    known = known_files(args.docs_dir)
    deny_terms, deny_warnings = load_deny_terms(args.docs_dir)
    clients = {name: VaultClient(args.api, key, timeout=120, transport=transport) for name, key in users.items()}
    try:
        try:   # the read phase: whatever goes wrong here, nothing was changed and nothing was judged
            libraries, skipped = gather(clients)
            if args.check and skipped:
                print(f"cannot check the vaults: {'; '.join(skipped)} (a vault the check cannot read is not clean)",
                      file=sys.stderr)
                return 2
            plan = plan_cleanup(libraries, known, test_titles=args.test_titles, deny_terms=deny_terms)
        except Exception as exc:  # noqa: BLE001 - any failure to read means "cannot check", never "plan above"
            print(f"cannot check the vaults at {args.api}: {describe(exc)}", file=sys.stderr)
            return 2
        plan.warnings[:0] = skipped + deny_warnings
        mode_label = "APPLY" if args.apply else ("CHECK" if args.check else "DRY RUN: nothing will change")
        print(f"Pre-demo plan for {args.api} ({mode_label})")
        print_known(known, args.docs_dir, plan.terms, no_user_docs=args.no_user_docs)
        if no_user_docs:                   # first, not last: it changes what the whole plan below means
            print(f"{paint('WARN')} {no_user_docs}; pass --no-user-docs if there is none")
        print_plan(plan, libraries, verbose=args.verbose)
        cases = None if args.check else backup_cases()
        backups = [] if cases is None else demo_backups(libraries, cases, plan)
        if cases is not None:
            print_backups(backups, {i: c.question for i, c in cases.items()}, hint=not args.apply)
        print_rehearsals(plan)
        print_summary(plan)
        if args.check:
            return check_verdict(plan)
        reviewed = plan_id(plan, backups)
        command = apply_command(args, argv, plan, reviewed)
        if not args.apply:
            print("DRY RUN: nothing was changed. " + ("Nothing to delete; to apply (e.g. with BACKUPS=1):"
                                                      if plan.empty else
                                                      f"To execute exactly this plan ({reviewed}; refused if "
                                                      "anything it deletes changes):")
                  + f"\n  {command}")
            return 0
        why = refusal(args, plan, reviewed)
        if why:
            print(f"\n{paint('FAIL')} refusing to apply, nothing was changed: {why}. Review the plan above; to "
                  f"execute exactly it:\n  {command}", file=sys.stderr)
            return 2
        try:
            return execute(args, clients, plan, known, deny_terms, cases or {}, failing_backups(backups))
        except (ApiError, httpx.HTTPError) as exc:
            print(f"\n{paint('FAIL')} API error after deleting started: {exc}; rerun make pre-demo to see what is "
                  "left", file=sys.stderr)
            return 1
    finally:
        for client in clients.values():
            client.close()


def check_verdict(plan: Plan) -> int:
    if plan.blocks_snapshot:
        leaky = sum(s.leaks for s in plan.session_deletes)
        print(f"{paint('FAIL')} non-corpus content is still in the vault ({len(plan.doc_deletes)} documents, "
              f"{len(plan.untrusted)} untrusted demo_docs files, {leaky} sessions that use or mention one): run the "
              "dry run (make pre-demo), then its apply command")
        return 1
    print(f"{paint('PASS')} only demo corpus content is left"
          + ("" if plan.empty else " (the sessions above are untidy, not a leak)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
