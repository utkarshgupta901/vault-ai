"""Load and validate the golden set (evals/golden.yaml, plus evals/golden_starters.yaml when it exists).

Validation is strict on purpose: a typo in a tag or an expectation key would otherwise silently drop a case from a
metric. `validate()` returns a list of human-readable problems; the tests assert it is empty."""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

EVALS_DIR = Path(__file__).resolve().parent
GOLDEN_PATH = EVALS_DIR / "golden.yaml"
STARTERS_PATH = EVALS_DIR / "golden_starters.yaml"

# Exactly one category tag per case (it decides which metric bucket the case lands in) ...
CATEGORY_TAGS = {"factoid", "multi_doc", "unanswerable_offtopic", "unanswerable_indomain", "multiturn", "injection",
                 "docx", "starter", "public_doc"}
# ... plus optional modifiers.
MODIFIER_TAGS = {"demo", "table", "hard"}
ALLOWED_TAGS = CATEGORY_TAGS | MODIFIER_TAGS
UNANSWERABLE_TAGS = {"unanswerable_offtopic", "unanswerable_indomain"}
# Which refusal layer we expect for each kind of unanswerable question (recorded, never failed on).
EXPECTED_REFUSAL_PATH = {"unanswerable_offtopic": "gate", "unanswerable_indomain": "model"}
# Cases over a real, public document the user drops into demo_docs/user/ (gitignored, never generated). Its file is
# not in `corpus`, and the eval runner skips these cases when the file is absent instead of failing the whole run.
PUBLIC_DOC_TAG = "public_doc"
# The hard set: paraphrases, "no" answers, computed numbers, cross-document questions, in-domain traps and a pronoun
# follow-up, all over the synthetic corpus. Scored and reported on their own line, so the headline over the original
# cases stays comparable with every earlier report. Never `demo`: a demo case must pass every run, a hard case may not.
HARD_TAG = "hard"

CASE_KEYS = {"id", "question", "tags", "scope", "turns", "expect", "retrieval_query", "note"}
REQUIRED_CASE_KEYS = {"id", "question", "tags", "scope", "expect"}
EXPECT_KEYS = {"any_of", "all_of", "forbid", "forbid_start", "must_cite", "gold_chunk", "not_found",
               "min_citations", "grounded"}
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


@dataclass
class Case:
    id: str
    question: str
    tags: list[str]
    scope: str | list[str]                 # "all" or a list of filenames
    expect: dict[str, Any]
    turns: list[str] = field(default_factory=list)
    retrieval_query: str | None = None     # standalone form used for retrieval metrics on multi-turn cases

    @property
    def category(self) -> str:
        return next((t for t in self.tags if t in CATEGORY_TAGS), "uncategorized")

    @property
    def is_demo(self) -> bool:
        return "demo" in self.tags

    @property
    def not_found(self) -> bool:
        return bool(self.expect.get("not_found", False))

    @property
    def is_public_doc(self) -> bool:
        return PUBLIC_DOC_TAG in self.tags

    @property
    def is_hard(self) -> bool:
        return HARD_TAG in self.tags

    @property
    def scope_files(self) -> list[str]:
        return [] if self.scope == "all" else list(self.scope)

    def search_query(self) -> str | None:
        """What to send to /search for retrieval metrics. A bare follow-up ('And CoreLink's?') is meaningless on its
        own, so multi-turn cases use their stated standalone form, or are skipped."""
        if self.turns:
            return self.retrieval_query
        return self.retrieval_query or self.question


@dataclass
class Golden:
    corpus: list[str]
    cases: list[Case]

    def referenced_files(self) -> list[str]:
        """Corpus files plus any file a case scopes to or must cite (e.g. the user's own document)."""
        names = list(self.corpus)
        for case in self.cases:
            must = case.expect.get("must_cite") or []
            for name in case.scope_files + ([must] if isinstance(must, str) else list(must)):
                if name not in names:
                    names.append(name)
        return names


def _case(raw: dict[str, Any]) -> Case:
    return Case(id=raw["id"], question=raw["question"], tags=list(raw["tags"]), scope=raw["scope"],
                expect=dict(raw.get("expect") or {}), turns=list(raw.get("turns") or []),
                retrieval_query=raw.get("retrieval_query"))


def load_raw(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if isinstance(data, list):                 # golden_starters.yaml is a bare list of cases
        data = {"cases": data}
    return data


def load(path: Path = GOLDEN_PATH, *, starters: Path | None = STARTERS_PATH) -> Golden:
    raw = load_raw(path)
    problems = validate(raw)
    if starters is not None and starters.exists():
        extra = load_raw(starters)
        problems += validate(extra, corpus=raw.get("corpus"), require_corpus=False)
        raw = {**raw, "cases": list(raw.get("cases") or []) + list(extra.get("cases") or [])}
        problems += _duplicate_ids(raw["cases"])
    if problems:
        raise ValueError("invalid golden set:\n  " + "\n  ".join(problems))
    return Golden(corpus=list(raw["corpus"]), cases=[_case(c) for c in raw["cases"]])


def _duplicate_ids(cases: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    dupes = []
    for c in cases:
        if c.get("id") in seen:
            dupes.append(f"duplicate id {c.get('id')!r}")
        seen.add(c.get("id"))
    return dupes


def _is_str_list(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(v, str) and v.strip() for v in value)


def _validate_expect(cid: str, tags: set[str], expect: Any, scope_files: list[str] | None) -> list[str]:
    if not isinstance(expect, dict):
        return [f"{cid}: expect must be a mapping"]
    problems = [f"{cid}: unknown expect key {k!r}" for k in expect if k not in EXPECT_KEYS]
    for key in ("any_of", "forbid", "forbid_start", "gold_chunk"):
        if key in expect and not _is_str_list(expect[key]):
            problems.append(f"{cid}: expect.{key} must be a non-empty list of strings")
    if "all_of" in expect:
        groups = expect["all_of"]
        if not isinstance(groups, list) or not groups or not all(
                (isinstance(g, str) and g.strip()) or _is_str_list(g) for g in groups):
            problems.append(f"{cid}: expect.all_of must be a list of strings or lists of strings")
    if "grounded" in expect and not isinstance(expect["grounded"], bool):      # "no" would be truthy and gate
        problems.append(f"{cid}: expect.grounded must be true or false")
    must = expect.get("must_cite")
    must_list = [must] if isinstance(must, str) else (must or [])
    if must is not None and not _is_str_list(must_list):
        problems.append(f"{cid}: expect.must_cite must be a filename or a list of filenames")
    if scope_files is not None:
        problems += [f"{cid}: must_cite {m!r} is not in the case scope" for m in must_list if m not in scope_files]
    unanswerable = bool(tags & UNANSWERABLE_TAGS)
    if unanswerable and expect.get("not_found") is not True:
        problems.append(f"{cid}: unanswerable cases need expect.not_found: true")
    if expect.get("not_found") and not (unanswerable or PUBLIC_DOC_TAG in tags):
        problems.append(f"{cid}: only unanswerable_* (or {PUBLIC_DOC_TAG}) cases may expect not_found: true")
    if expect.get("not_found"):
        problems += [f"{cid}: a not_found case cannot have expect.{k}" for k in ("any_of", "all_of", "must_cite",
                                                                                 "gold_chunk") if k in expect]
    elif "starter" not in tags:
        if not (expect.get("any_of") or expect.get("all_of")):
            problems.append(f"{cid}: answerable cases need expect.any_of or expect.all_of")
        if not expect.get("gold_chunk"):
            problems.append(f"{cid}: answerable cases need expect.gold_chunk (for retrieval metrics)")
    return problems


def _validate_case(c: Any, corpus: list[str], *, check_files: bool) -> list[str]:
    if not isinstance(c, dict):
        return [f"case is not a mapping: {c!r}"]
    cid = str(c.get("id", "<missing id>"))
    problems = [f"{cid}: missing required key {k!r}" for k in sorted(REQUIRED_CASE_KEYS - c.keys())]
    problems += [f"{cid}: unknown key {k!r}" for k in c if k not in CASE_KEYS]
    if problems:
        return problems
    if not ID_RE.match(cid):
        problems.append(f"{cid}: id must be lowercase letters, digits and dashes")
    if not isinstance(c["question"], str) or not c["question"].strip():
        problems.append(f"{cid}: question must be a non-empty string")
    tags = c["tags"]
    if not _is_str_list(tags):
        return problems + [f"{cid}: tags must be a non-empty list"]
    problems += [f"{cid}: unknown tag {t!r}" for t in tags if t not in ALLOWED_TAGS]
    categories = [t for t in tags if t in CATEGORY_TAGS]
    if len(categories) != 1:
        problems.append(f"{cid}: needs exactly one category tag from {sorted(CATEGORY_TAGS)}, got {categories}")
    if "demo" in tags and HARD_TAG in tags:
        problems.append(f"{cid}: a case cannot be both demo and hard")
    scope = c["scope"]
    scope_files: list[str] | None = None
    if scope == "all":
        scope_files = list(corpus)
    elif _is_str_list(scope):
        scope_files = list(scope)
        if check_files:
            problems += [f"{cid}: scope file {f!r} is not in the corpus" for f in scope if f not in corpus
                         and PUBLIC_DOC_TAG not in tags and "starter" not in tags]
    else:
        problems.append(f"{cid}: scope must be 'all' or a non-empty list of filenames")
    turns = c.get("turns")
    if turns is not None and not _is_str_list(turns):
        problems.append(f"{cid}: turns must be a non-empty list of questions")
    if "multiturn" in tags and not turns:
        problems.append(f"{cid}: multiturn cases need prior turns")
    problems += _validate_expect(cid, set(tags), c["expect"], scope_files if check_files else None)
    return problems


def validate(raw: dict[str, Any], *, corpus: list[str] | None = None, require_corpus: bool = True) -> list[str]:
    """Return every problem found (empty list = valid)."""
    corpus = list(corpus if corpus is not None else raw.get("corpus") or [])
    problems: list[str] = []
    if require_corpus and not _is_str_list(raw.get("corpus")):
        problems.append("top-level 'corpus' must list the files to upload")
    cases = raw.get("cases")
    if not isinstance(cases, list) or not cases:
        return problems + ["top-level 'cases' must be a non-empty list"]
    for c in cases:
        problems += _validate_case(c, corpus, check_files=bool(corpus))
    return problems + _duplicate_ids(cases)
