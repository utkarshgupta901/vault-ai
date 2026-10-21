"""Evaluate Vault end to end over HTTP against the golden set (evals/golden.yaml + golden_starters.yaml).

    uv run python evals/run_eval.py --api http://localhost:8000 --key vault-eval-key
        [--tag demo] [--repeat 3] [--retrieval-only] [--modes hybrid,vector,keyword] [--k 40] [--out evals/reports]
        [--judge]

What it does, in order:
  1. Preflight: /health/ready (records server model names) and which routes exist (/openapi.json).
  2. Corpus: uploads the corpus files (plus any real document in demo_docs/user/) for the eval user (duplicates are
     free) and waits until they are ready. Cases tagged `public_doc` are skipped when their document is absent.
  3. Retrieval (no LLM, ~free): GET /search per mode for every case with gold_chunk labels; hit@1/5/8/40 and MRR
     per mode x tag, with raw counts.
  4. Answers: a NEW chat session per case run (scope from the case), prior turns asked first, then the question via
     the JSON endpoint. Each answer is scored by evals/scoring.score_answer. Cases tagged `demo` run --repeat times
     and must pass every run (pass^N); quality metrics use each case's first run so every case weighs the same,
     while latency and cost count every run.
     Cases tagged `hard` are summarized apart (retrieval and answers), so the headline over the original cases
     stays comparable with every earlier report.
  5. With --judge: an LLM judge (evals/judge.py) grades each model-generated answer against the case's golden facts,
     and the report gives its agreement with the code's content checks plus every disagreement. Never gates.
  6. Report: evals/reports/<UTC timestamp>.json (everything, including answers) and .md (the readable summary the
     README quotes), and the markdown is printed.

Exit code 1 when the corpus could not be prepared, a needed endpoint is missing, --judge was asked for and the judge
cannot run, or a demo case failed any run.
The eval user (vault-eval-key) is separate from the demo user so eval spend never eats the demo user's quota."""

import argparse
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from evals import golden as golden_mod  # noqa: E402
from evals import scoring  # noqa: E402
from evals.api_client import API_PREFIX, ApiError, VaultClient  # noqa: E402
from evals.corpus import DEMO_DOCS, ROOT, resolve, user_files  # noqa: E402
from evals.golden import EXPECTED_REFUSAL_PATH, Case, Golden  # noqa: E402
from evals.report import render_markdown, write_report  # noqa: E402

DEFAULT_MODES = ("hybrid", "vector", "keyword")
FALLBACK_K = 8
SEARCH_ROUTE = f"{API_PREFIX}/search"
CHAT_ROUTE = f"{API_PREFIX}/chat/sessions"
USAGE_ROUTE = f"{API_PREFIX}/metrics/ai-usage"
REPORTS_DIR = Path("evals") / "reports"         # relative to ROOT


class SetupError(Exception):
    """The eval cannot run meaningfully (missing corpus file, document failed to ingest, endpoint absent)."""


# (case: {"id", "question", "turns", "expect"}, first run as stored in the report) -> {"correct", "rationale", "model"}
JudgeFn = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]


@dataclass
class Options:
    tags: list[str]
    case_ids: list[str]
    repeat: int
    retrieval_only: bool
    modes: list[str]
    k: int
    docs_dir: Path
    ingest_timeout_s: float
    judge: JudgeFn | None = None


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------------------------------------------
def select_cases(cases: list[Case], tags: list[str], case_ids: list[str]) -> list[Case]:
    selected = [c for c in cases if not tags or set(tags) & set(c.tags)]
    return [c for c in selected if not case_ids or c.id in case_ids]


def available_cases(cases: list[Case], docs_dir: Path) -> tuple[list[Case], list[str]]:
    """Split off `public_doc` cases whose document is not on this machine (demo_docs/user/ is gitignored and filled by
    hand), so a fresh clone still runs the synthetic set. Any other missing file is a real error: prepare_corpus
    reports it. Returns (cases to run, ids of skipped cases)."""
    kept, skipped = [], []
    for case in cases:
        if case.is_public_doc and any(resolve(name, docs_dir) is None for name in case.scope_files):
            skipped.append(case.id)
        else:
            kept.append(case)
    return kept, skipped


def needed_files(golden: Golden, cases: list[Case], docs_dir: Path) -> list[str]:
    """The base corpus (so 'all' scope means the same thing every run), anything the selected cases name, and the
    user's real documents (seed_demo.py uploads them for the demo user too, so an all-documents session here sees the
    same vault as the demo)."""
    names = Golden(corpus=golden.corpus, cases=cases).referenced_files()
    return list(dict.fromkeys(names + [p.name for p in user_files(docs_dir)]))


def prepare_corpus(client: VaultClient, names: list[str], docs_dir: Path, timeout_s: float) -> dict[str, str]:
    """Upload + wait. Returns {filename: document id}; raises SetupError if anything is missing or not ready."""
    paths = {name: resolve(name, docs_dir) for name in names}
    missing = [n for n, p in paths.items() if p is None]
    if missing:
        raise SetupError(f"corpus files not found in {docs_dir} (run scripts/make_demo_docs.py?): {missing}")
    log(f"corpus: uploading {len(paths)} files and waiting until ready ...")
    docs = client.ensure_uploaded([p for p in paths.values() if p], timeout_s=timeout_s)
    not_ready = {n: (d.get("status"), (d.get("error") or {}).get("code")) for n, d in docs.items()
                 if d.get("status") != "ready"}
    if not_ready:
        raise SetupError(f"corpus documents not ready: {not_ready}")
    return {name: doc["id"] for name, doc in docs.items()}


def scope_ids(case: Case, doc_ids: dict[str, str]) -> list[str] | None:
    """None = all documents (an all-documents session / an unfiltered search)."""
    return [doc_ids[f] for f in case.scope_files] or None


def today_spend(client: VaultClient) -> float | None:
    if not client.has_route(USAGE_ROUTE):
        return None
    try:
        return float(client.get_json(USAGE_ROUTE, {"window": "24h"}).get("today_spend_usd"))
    except (ApiError, TypeError, ValueError):
        return None


def _git(*args: str) -> str | None:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def git_sha() -> str | None:
    return _git("rev-parse", "--short", "HEAD")


def git_dirty() -> bool | None:
    """True when the checkout has uncommitted or untracked changes: then the sha alone doesn't say what code produced
    the numbers. None when git isn't available. The reports the eval itself writes don't count: they are committed
    next to the code, so the previous run's files would otherwise mark every following run dirty.

    Both describe the eval CLIENT's checkout. The API answers with whatever its container was built from: after
    merging, rebuild it (`make demo`) before quoting a report as that commit's numbers."""
    status = _git("status", "--porcelain", "--", ".", f":(exclude){REPORTS_DIR.as_posix()}")
    return None if status is None else bool(status)


# ---------------------------------------------------------------------------------------------------------------
# Retrieval metrics
# ---------------------------------------------------------------------------------------------------------------
def search_case(client: VaultClient, case: Case, ids: list[str] | None, mode: str, k: int) -> dict[str, Any]:
    query = case.search_query() or ""
    resp = client.search(query, document_ids=ids, mode=mode, k=k)
    hits = resp.get("hits") or []
    rank = scoring.first_relevant_rank([h.get("content") or h.get("snippet") or "" for h in hits],
                                       case.expect["gold_chunk"])
    gold_hit = hits[rank - 1] if rank else {}
    return {"case_id": case.id, "tags": case.tags, "mode": mode, "query": query, "rank": rank, "n_hits": len(hits),
            "top_similarity": resp.get("top_similarity"), "gold_lex_rank": gold_hit.get("lex_rank"),
            "gold_sem_rank": gold_hit.get("sem_rank"), "gold_id_hit": gold_hit.get("id_hit"),
            "gold_filename": gold_hit.get("filename"), "gold_label": gold_hit.get("label")}


def run_retrieval(client: VaultClient, cases: list[Case], doc_ids: dict[str, str], modes: list[str],
                  k: int) -> tuple[list[dict[str, Any]], int]:
    """Returns (records, the k actually used). If the server rejects k (e.g. a lower max), fall back to k=8 once."""
    records = []
    for case in cases:
        if not case.expect.get("gold_chunk") or not case.search_query():
            continue
        for mode in modes:
            try:
                records.append(search_case(client, case, scope_ids(case, doc_ids), mode, k))
            except ApiError as exc:
                if exc.status != 422 or k == FALLBACK_K:
                    raise
                log(f"search rejected k={k}; falling back to k={FALLBACK_K}")
                k = FALLBACK_K
                records.append(search_case(client, case, scope_ids(case, doc_ids), mode, k))
    return records, k


def is_hard(record: dict[str, Any]) -> bool:
    return golden_mod.HARD_TAG in record["tags"]


def summarize_retrieval(records: list[dict[str, Any]], ks: tuple[int, ...]) -> dict[str, Any]:
    """{mode: {"overall": metrics, "by_tag": {tag: metrics}}} where metrics = hit@k counts + MRR."""
    out: dict[str, Any] = {}
    for mode in dict.fromkeys(r["mode"] for r in records):
        rows = [r for r in records if r["mode"] == mode]
        tags = sorted({t for r in rows for t in r["tags"]})
        out[mode] = {
            "overall": scoring.retrieval_metrics([r["rank"] for r in rows], ks),
            "by_tag": {t: scoring.retrieval_metrics([r["rank"] for r in rows if t in r["tags"]], ks) for t in tags},
        }
    return out


# ---------------------------------------------------------------------------------------------------------------
# Answer metrics
# ---------------------------------------------------------------------------------------------------------------
def _num(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def answer_record(answer: dict[str, Any]) -> dict[str, Any]:
    """What the report keeps of an answer: enough to re-score it, audit it by hand, or judge it later."""
    return {
        "content": answer.get("content"), "answer_type": answer.get("answer_type"),
        "confidence": answer.get("confidence"), "signals": answer.get("signals") or {},
        "standalone_query": answer.get("standalone_query"), "warnings": answer.get("warnings") or [],
        "citations": [{k: c.get(k) for k in ("ref", "filename", "label", "page_start", "snippet", "weak_support")}
                      for c in answer.get("citations") or []],
        "model": answer.get("model"), "prompt_version": answer.get("prompt_version"),
        "ttft_ms": answer.get("ttft_ms"), "latency_ms": answer.get("latency_ms"),
        "cost_usd": _num(answer.get("cost_usd")),
    }


def score_run(case: Case, answer: dict[str, Any]) -> dict[str, Any]:
    """score_answer plus the runner's guard on the contract it relies on: an answer without the signals the checks
    read fails as such, with the reason, so a server that dropped or renamed `signals` cannot look clean."""
    scored = scoring.score_answer(case.expect, answer)
    missing = scoring.missing_signals(answer)
    if not missing:
        return {**scored, "error": None}
    return {**scored, "passed": False, "failed": ["signals", *scored["failed"]],
            "error": f"answer lacks signals {missing}: the citation and grounding checks had nothing to read"}


def run_case_once(client: VaultClient, case: Case, ids: list[str] | None) -> dict[str, Any]:
    """One fresh session: prior turns first (so condensation has history), then the scored question."""
    try:
        session = client.create_session(ids, title=f"eval {case.id}")
        turns = []
        for turn in case.turns:
            prior = client.ask(session["id"], turn)["answer"]
            turns.append({"question": turn, "answer_type": prior.get("answer_type"),
                          "content": (prior.get("content") or "")[:400]})
        started = time.perf_counter()
        answer = client.ask(session["id"], case.question)["answer"]
        client_ms = int((time.perf_counter() - started) * 1000)
    except (ApiError, httpx.HTTPError) as exc:      # one slow or failed answer must not sink the whole run
        return {"passed": False, "failed": ["error"], "checks": {}, "error": str(exc), "answer": None, "turns": []}
    return {**score_run(case, answer), "answer": answer_record(answer), "client_ms": client_ms,
            "session_id": session["id"], "turns": turns}


def run_answers(client: VaultClient, cases: list[Case], doc_ids: dict[str, str], repeat: int) -> dict[str, list]:
    """{case id: [run, ...]}: demo cases get `repeat` runs, others one."""
    results: dict[str, list[dict[str, Any]]] = {}
    for case in cases:
        n = repeat if case.is_demo else 1
        results[case.id] = []
        for i in range(n):
            run = run_case_once(client, case, scope_ids(case, doc_ids))
            results[case.id].append(run)
            ms = run.get("client_ms")
            log(f"  {'PASS' if run['passed'] else 'FAIL'} {case.id} [{i + 1}/{n}]"
                f"{f' {ms} ms' if ms else ''}{'' if run['passed'] else ' ' + ','.join(run['failed'])}")
    return results


def _rate(values: list[bool]) -> dict[str, Any]:
    return {"ok": sum(values), "n": len(values), "rate": sum(values) / len(values) if values else None}


def summarize_answers(cases: list[Case], results: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Every number over `cases` only, runs included: the headline and the hard set are summarized separately."""
    results = {c.id: results[c.id] for c in cases if c.id in results}
    first = {c.id: results[c.id][0] for c in cases if results.get(c.id)}
    by_id = {c.id: c for c in cases}
    answered = {cid: r for cid, r in first.items() if r["answer"] is not None}

    def check_rate(name: str) -> dict[str, Any]:
        return _rate([r["checks"][name] for r in answered.values() if r["checks"].get(name) is not None])

    tags = sorted({t for c in cases for t in c.tags})
    refusals = [{"expected": by_id[cid].not_found, "predicted": r["answer"]["answer_type"] == "not_found",
                 "path": scoring.refusal_path(r["answer"]),
                 "expected_path": EXPECTED_REFUSAL_PATH.get(by_id[cid].category)} for cid, r in answered.items()]
    all_runs = [run for runs in results.values() for run in runs if run["answer"] is not None]
    model_runs = [run for run in all_runs if scoring.model_generated(run["answer"])]
    costs = [run["answer"]["cost_usd"] for run in all_runs if run["answer"]["cost_usd"] is not None]
    ttft_all = scoring.latency_summary([run["answer"]["ttft_ms"] for run in all_runs])
    return {
        "cases": len(first),
        "passed": _rate([r["passed"] for r in first.values()]),          # the headline: first run of each case
        "passed_all_runs": _rate([run["passed"] for runs in results.values() for run in runs]),
        "by_tag": {t: _rate([first[c.id]["passed"] for c in cases if t in c.tags and c.id in first]) for t in tags},
        "checks": {name: check_rate(name) for name in ("not_found", "citations_valid", "gold_citation", "grounded")},
        "errors": [cid for cid, r in first.items() if r["error"]],
        "refusals": scoring.refusal_stats(refusals),
        "reliability": scoring.reliability_table(
            [{"confidence": r["answer"]["confidence"], "passed": r["passed"]} for r in answered.values()]),
        "demo": [{"id": c.id, **scoring.stability([run["passed"] for run in results[c.id]])}
                 for c in cases if c.is_demo and results.get(c.id)],
        "latency_ms": {
            # Gate refusals reply in ~10 ms without a model call, so TTFT over all answers flatters the model.
            "ttft_model": scoring.latency_summary([run["answer"]["ttft_ms"] for run in model_runs]),
            "ttft_all": ttft_all,
            "ttft": ttft_all,           # = ttft_all, under the name reports before 2026-10-14 used
            "server_total": scoring.latency_summary([run["answer"]["latency_ms"] for run in all_runs]),
            "client_total": scoring.latency_summary([run.get("client_ms") for run in all_runs]),
        },
        "cost_usd": {"answers": len(all_runs), "total": round(sum(costs), 6),
                     "per_answer": round(sum(costs) / len(costs), 6) if costs else None},
        "server": {
            "models": dict(Counter(run["answer"]["model"] or "unknown" for run in all_runs)),
            "prompt_versions": dict(Counter(run["answer"]["prompt_version"] or "unknown" for run in all_runs)),
        },
    }


def run_judge(cases: list[Case], results: dict[str, list[dict[str, Any]]], judge: JudgeFn) -> dict[str, Any]:
    """The judge's verdict on each case's first model-generated answer (refusals and failed calls have nothing to
    grade), next to the code's content verdict. A judge failure is recorded on its row and never stops the run: the
    judge is a second opinion on the scorer, not part of it."""
    rows = []
    for case in cases:
        run = (results.get(case.id) or [None])[0]
        if not run or not run["answer"] or run["answer"]["answer_type"] != "answered":
            continue
        row = {"id": case.id, "set": "hard" if case.is_hard else "core", "code": scoring.content_verdict(run["checks"]),
               "judge": None, "failed": run["failed"], "rationale": None, "model": None, "error": None}
        try:
            verdict = judge({"id": case.id, "question": case.question, "turns": case.turns, "expect": case.expect},
                            run)
            row.update(judge=bool(verdict["correct"]), rationale=verdict.get("rationale"), model=verdict.get("model"))
        except Exception as exc:        # any judge failure (provider, parsing, ledger): recorded, never fatal
            row["error"] = f"{type(exc).__name__}: {exc}"
        rows.append(row)
        log(f"  judge {case.id}: {row['error'] or ('pass' if row['judge'] else 'fail')}"
            f" (code {'pass' if row['code'] else 'fail'})")
    return {"summary": scoring.judge_agreement(rows), "rows": rows}


# ---------------------------------------------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------------------------------------------
def preflight(client: VaultClient, opts: Options) -> dict[str, Any]:
    ready = client.request("GET", "/health/ready", ok=(200, 503)).json()
    if not opts.retrieval_only and not client.has_route(CHAT_ROUTE):
        raise SetupError(f"{CHAT_ROUTE} is not in /openapi.json: the chat API is not deployed (use --retrieval-only)")
    return {"status": ready.get("status"), "models": ready.get("models")}


def evaluate(client: VaultClient, golden: Golden, opts: Options) -> dict[str, Any]:
    """Run the whole evaluation and return the report dict (no files written here)."""
    started = datetime.now(UTC)
    cases, skipped = available_cases(select_cases(golden.cases, opts.tags, opts.case_ids), opts.docs_dir)
    if skipped:
        log(f"skipping {len(skipped)} public_doc case(s), document not in {opts.docs_dir / 'user'}: {skipped}")
    if not cases:
        raise SetupError(f"no runnable cases match tags={opts.tags} ids={opts.case_ids}")
    health = preflight(client, opts)
    doc_ids = prepare_corpus(client, needed_files(golden, cases, opts.docs_dir), opts.docs_dir,
                             opts.ingest_timeout_s)
    spend_before = today_spend(client)
    ks = (1, 5, 8, opts.k) if opts.k > 8 else (1, 5, 8)

    retrieval: dict[str, Any]
    if client.has_route(SEARCH_ROUTE):
        log(f"retrieval: {len(cases)} cases x modes {opts.modes}")
        records, k_used = run_retrieval(client, cases, doc_ids, opts.modes, opts.k)
        ks = tuple(k for k in ks if k <= k_used)
        retrieval = {"k": k_used, "ks": list(ks),
                     "summary": summarize_retrieval([r for r in records if not is_hard(r)], ks), "records": records}
        if any(is_hard(r) for r in records):
            retrieval["hard"] = summarize_retrieval([r for r in records if is_hard(r)], ks)
    else:
        retrieval = {"skipped": f"{SEARCH_ROUTE} is not deployed"}

    answers: dict[str, Any]
    results: dict[str, list[dict[str, Any]]] = {}
    if opts.retrieval_only:
        answers = {"skipped": "--retrieval-only"}
    else:
        log(f"answers: {len(cases)} cases (demo cases x{opts.repeat})")
        results = run_answers(client, cases, doc_ids, opts.repeat)
        answers = summarize_answers([c for c in cases if not c.is_hard], results)
        if any(c.is_hard for c in cases):
            answers["hard"] = summarize_answers([c for c in cases if c.is_hard], results)
    judged = None
    if opts.judge and results:
        log(f"judge: grading the first model-generated answer of each of {len(cases)} cases")
        judged = run_judge(cases, results, opts.judge)
    spend_after = today_spend(client)

    report = {
        "meta": {
            "started_at": started.isoformat(timespec="seconds"),
            "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "api": client.api, "git_sha": git_sha(), "git_dirty": git_dirty(), "tags": opts.tags,
            "case_ids": opts.case_ids,
            "repeat": opts.repeat, "modes": opts.modes, "retrieval_only": opts.retrieval_only,
            "cases": len(cases), "skipped_cases": skipped, "health": health, "corpus": doc_ids,
            "spend_usd": {"before": spend_before, "after": spend_after,
                          "delta": (round(spend_after - spend_before, 6)
                                    if spend_before is not None and spend_after is not None else None)},
        },
        "retrieval": retrieval,
        "answers": answers,
        "cases": [{"id": c.id, "category": c.category, "tags": c.tags, "question": c.question, "turns": c.turns,
                   "scope": c.scope, "expect": c.expect, "runs": results.get(c.id, [])} for c in cases],
    }
    if judged is not None:
        report["judge"] = judged
    return report


def demo_failures(report: dict[str, Any]) -> list[str]:
    return [d["id"] for d in report["answers"].get("demo", []) if not d["stable"]]


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Evaluate Vault against the golden set over HTTP.")
    p.add_argument("--api", default="http://localhost:8000")
    p.add_argument("--key", default="vault-eval-key", help="API key of the eval user")
    p.add_argument("--tag", default="", help="comma-separated tags; a case runs if it has any of them")
    p.add_argument("--case", default="", help="comma-separated case ids (debugging)")
    p.add_argument("--repeat", type=int, default=1, help="runs per demo-tagged case (must pass every run)")
    p.add_argument("--retrieval-only", action="store_true", help="only /search metrics (no answer-model spend)")
    p.add_argument("--modes", default=",".join(DEFAULT_MODES), help="retrieval modes for /search metrics")
    p.add_argument("--k", type=int, default=40, help="hits requested per search (hit@k up to this)")
    p.add_argument("--out", type=Path, default=ROOT / REPORTS_DIR)
    p.add_argument("--golden", type=Path, default=golden_mod.GOLDEN_PATH)
    p.add_argument("--no-starters", action="store_true", help="ignore evals/golden_starters.yaml")
    p.add_argument("--docs-dir", type=Path, default=DEMO_DOCS)
    p.add_argument("--ingest-timeout", type=float, default=600, help="seconds to wait for the corpus to be ready")
    p.add_argument("--judge", action="store_true",
                   help="also grade each answer with the LLM judge and report its agreement with the code (never "
                        "gates; needs a model key and the database the API writes its ai_usage ledger to)")
    return p.parse_args(argv)


def _csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def live_judge() -> JudgeFn:
    """The real judge, checked before the eval spends anything. Imported here, not at the top: a plain run is an HTTP
    client and must not need the app's settings, database or model key."""
    from evals import judge

    return judge.correctness_judge()


def main(argv: list[str] | None = None, *, transport: Any = None, judge_fn: JudgeFn | None = None) -> int:
    args = parse_args(argv)
    golden = golden_mod.load(args.golden, starters=None if args.no_starters else golden_mod.STARTERS_PATH)
    if args.judge and judge_fn is None:
        try:
            judge_fn = live_judge()
        except Exception as exc:        # no key, no ledger database, no eval user: say so before any spend
            log(f"--judge cannot run: {type(exc).__name__}: {exc}")
            return 1
    opts = Options(tags=_csv(args.tag), case_ids=_csv(args.case), repeat=max(1, args.repeat),
                   retrieval_only=args.retrieval_only, modes=_csv(args.modes), k=args.k, docs_dir=args.docs_dir,
                   ingest_timeout_s=args.ingest_timeout, judge=judge_fn if args.judge else None)
    with VaultClient(args.api, args.key, timeout=120, transport=transport) as client:
        try:
            report = evaluate(client, golden, opts)
        except (SetupError, ApiError, httpx.HTTPError) as exc:
            log(f"eval failed: {exc}")
            return 1
    json_path, md_path = write_report(report, args.out)
    print(render_markdown(report))
    log(f"report: {json_path} and {md_path}")
    failures = demo_failures(report)
    if failures:
        log(f"demo cases not stable ({opts.repeat}/{opts.repeat} required): {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
