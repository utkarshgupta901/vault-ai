"""Re-score the answers stored in an eval report with the CURRENT scorer and golden labels, without calling the API.

    uv run python evals/rescore.py evals/reports/<timestamp>.json [--json out.json]

Use it after changing evals/scoring.py or evals/golden.yaml: it shows exactly which stored verdicts the change flips
and why (which check changed, and for phrase checks the words that matched or the reason an occurrence didn't count),
before anyone spends a cent on a new run. It is a re-score of old answers, not a new measurement: quote it as such.

Each run is scored by run_eval.score_run against the case's current expectations (the stored ones when the case is
no longer in the golden set). Runs without an answer (a failed call) are skipped."""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals import golden as golden_mod  # noqa: E402
from evals.golden import Case  # noqa: E402
from evals.run_eval import score_run  # noqa: E402


def _verdict(passed: bool) -> str:
    return "pass" if passed else "fail"


def _phrase_note(scored: dict[str, Any], check: str) -> str:
    items = scored.get("evidence", {}).get(check) or []
    notes = [f"'{e['phrase']}' matched '{e['matched']}'" if "matched" in e else f"'{e['phrase']}' {e['rejected']}"
             for e in items]
    return f" ({'; '.join(notes)})" if notes else ""


def explain(run: dict[str, Any], scored: dict[str, Any], labels_changed: bool) -> list[str]:
    """Why a stored verdict and the new one differ: each check whose value changed, each check that now (or no
    longer) decides the verdict with the same value (e.g. newly gating `grounded`), and whether the labels moved."""
    old_checks, old_failed = run.get("checks") or {}, set(run.get("failed") or [])
    why = []
    for name, value in scored["checks"].items():
        if name in old_checks and old_checks[name] is not None and value is not None and old_checks[name] != value:
            why.append(f"{name}: {_verdict(old_checks[name])} -> {_verdict(value)}{_phrase_note(scored, name)}")
    explained = {w.split(":")[0] for w in why}
    for name in sorted((old_failed ^ set(scored["failed"])) - explained):
        why.append(f"{name}: {'now fails' if name in scored['failed'] else 'no longer fails'}")
    if labels_changed:
        why.append("labels differ from the stored ones")
    return why


def rescore(report: dict[str, Any], golden: golden_mod.Golden | None = None) -> list[dict[str, Any]]:
    """One row per stored run that has an answer: stored verdict, new verdict, and why they differ."""
    current = {c.id: c for c in (golden or golden_mod.load(starters=None)).cases}
    rows = []
    for stored in report["cases"]:
        case = current.get(stored["id"]) or Case(id=stored["id"], question=stored.get("question", ""),
                                                  tags=stored.get("tags") or [], scope="all",
                                                  expect=stored["expect"])
        for i, run in enumerate(stored["runs"], start=1):
            if not run.get("answer"):
                continue
            scored = score_run(case, run["answer"])
            changed = bool(run["passed"]) != scored["passed"]
            rows.append({"id": stored["id"], "run": i, "old": _verdict(run["passed"]), "new": _verdict(scored["passed"]),
                         "changed": changed, "old_failed": run.get("failed") or [], "new_failed": scored["failed"],
                         "why": explain(run, scored, case.expect != stored["expect"]) if changed else []})
    return rows


def first_runs(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    first = [r for r in rows if r["run"] == 1]
    return {"stored": {"ok": sum(r["old"] == "pass" for r in first), "n": len(first)},
            "now": {"ok": sum(r["new"] == "pass" for r in first), "n": len(first)}}


def render(rows: list[dict[str, Any]], source: str) -> str:
    totals = first_runs(rows)
    changed = [r for r in rows if r["changed"]]
    lines = [f"# Re-score of `{source}` (stored answers, current scorer and labels, no API calls)", "",
             f"- First runs: {totals['stored']['ok']}/{totals['stored']['n']} passed as stored, "
             f"{totals['now']['ok']}/{totals['now']['n']} now",
             f"- Runs re-scored: {len(rows)}; verdicts changed: {len(changed)}", ""]
    if changed:
        lines += ["| run | stored | now | why |", "|---|---|---|---|"]
        lines += [f"| {r['id']} #{r['run']} | {r['old']} | {r['new']} | {'; '.join(r['why']).replace('|', '/')} |"
                  for r in changed]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Re-score a stored eval report offline with the current scorer.")
    p.add_argument("report", type=Path)
    p.add_argument("--golden", type=Path, default=golden_mod.GOLDEN_PATH)
    p.add_argument("--json", type=Path, help="also write the rows and totals here")
    args = p.parse_args(argv)
    report = json.loads(args.report.read_text(encoding="utf-8"))
    rows = rescore(report, golden_mod.load(args.golden, starters=None))
    print(render(rows, args.report.name))
    if args.json:
        args.json.write_text(json.dumps({"source": str(args.report), "first_runs": first_runs(rows), "rows": rows},
                                        indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
