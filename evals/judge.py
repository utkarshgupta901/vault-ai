"""Offline LLM judge: claim-level faithfulness of each answer in an eval report, against the snippets it cited.

    uv run python evals/judge.py evals/reports/<timestamp>.json [--limit 20] [--cases id1,id2]

For every answered case (first run), the judge model (settings.JUDGE_MODEL / JUDGE_EFFORT, through the app's
LLMProvider) splits the answer into atomic factual claims and labels each one supported / unsupported /
contradicted by the cited snippets. The SCORE is computed here in code (supported claims / all claims), not by the
model, so the arithmetic is auditable. Results go next to the report as <timestamp>.judged.json and .judged.md; the
Markdown has an empty "human" column for the hand audit of 12 verdicts (agreement goes into docs/EVAL.md).

Why it's offline: it costs a reasoning-model call per answer (~$0.03) and adds latency and a failure point; the live
path uses the deterministic citation/grounding checks instead. Never runs in tests (it needs a real key).
Every judge call is written to the ai_usage ledger (operation 'judge', owner = the eval user), like any model call."""

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Literal

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import BaseModel, Field  # noqa: E402
from sqlalchemy import select, text  # noqa: E402

from app.ai import get_provider  # noqa: E402
from app.ai.prompts.common import UNTRUSTED_RULE, escape  # noqa: E402
from app.ai.provider import Usage  # noqa: E402
from app.ai.usage import usage_row  # noqa: E402
from app.config import settings  # noqa: E402
from app.core.db import sync_session  # noqa: E402
from app.models import User  # noqa: E402

PROMPT_VERSION = "judge.v1"
AUDIT_ROWS = 12

INSTRUCTIONS = f"""You are a strict evaluator of answers produced by a document question-answering system.
Split the ANSWER into atomic factual claims (one fact each; ignore greetings, hedges and statements about what the
documents do not contain). For each claim decide, using ONLY the cited snippets inside <sources>:
- "supported": the snippets state it, or it follows by simple arithmetic shown in the answer from stated numbers;
- "contradicted": the snippets state something incompatible with it;
- "unsupported": the snippets neither state nor contradict it.
List the source ids that support or contradict each claim. Judge faithfulness to the snippets, not real-world truth.
Also say whether the answer addresses the question.
{UNTRUSTED_RULE}"""


class ClaimVerdict(BaseModel):
    claim: str
    verdict: Literal["supported", "unsupported", "contradicted"]
    source_ids: list[str] = Field(description="ids like S1 that support or contradict the claim")


class JudgeVerdict(BaseModel):
    claims: list[ClaimVerdict]
    addresses_question: bool
    rationale: str = Field(description="one sentence")


def judge_input(question: str, answer: str, citations: list[dict[str, Any]]) -> str:
    """User turn for the judge. Every piece of document or answer text is escaped inside tags (CLAUDE.md rule 5)."""
    sources = "\n".join(
        f'<source id="{escape(c.get("ref"))}" document="{escape(c.get("filename"))}" '
        f'location="{escape(c.get("label"))}">{escape(c.get("snippet") or "(no snippet)")}</source>'
        for c in citations)
    return (f"<question>{escape(question)}</question>\n<answer>{escape(answer)}</answer>\n"
            f"<sources>\n{sources}\n</sources>\nJudge the answer's claims against the sources.")


def faithfulness(verdict: JudgeVerdict) -> float:
    """Share of claims supported by the cited snippets; 1.0 when the answer makes no factual claims."""
    if not verdict.claims:
        return 1.0
    return sum(1 for c in verdict.claims if c.verdict == "supported") / len(verdict.claims)


def judgeable(report: dict[str, Any], case_ids: list[str]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """(case, first run) pairs whose answer was a real, cited answer (refusals have nothing to judge)."""
    pairs = []
    for case in report["cases"]:
        if (case_ids and case["id"] not in case_ids) or not case["runs"]:
            continue
        answer = case["runs"][0].get("answer")
        if answer and answer.get("answer_type") == "answered" and answer.get("citations"):
            pairs.append((case, case["runs"][0]))
    return pairs


def eval_user_id() -> Any:
    with sync_session() as s:
        return s.scalar(select(User.id).where(User.name == "eval"))


def record_usage(usage: Usage, owner_id: Any) -> None:
    with sync_session() as s:
        s.add(usage_row(operation="judge", usage=usage, owner_id=owner_id, prompt_version=PROMPT_VERSION))


def judge_case(case: dict[str, Any], run: dict[str, Any], owner_id: Any) -> dict[str, Any]:
    answer = run["answer"]
    verdict, usage = get_provider().complete_json(
        model=settings.JUDGE_MODEL, effort=settings.JUDGE_EFFORT, instructions=INSTRUCTIONS,
        input=judge_input(case["question"], answer["content"] or "", answer["citations"]),
        schema=JudgeVerdict, max_output_tokens=8000)
    record_usage(usage, owner_id)
    return {"id": case["id"], "question": case["question"], "answer": answer["content"],
            "faithfulness": faithfulness(verdict), "addresses_question": verdict.addresses_question,
            "claims": [c.model_dump() for c in verdict.claims], "rationale": verdict.rationale,
            "eval_passed": run["passed"], "model": usage.model, "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens}


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(results)
    claims = [c for r in results for c in r["claims"]]
    return {"answers": n,
            "mean_faithfulness": sum(r["faithfulness"] for r in results) / n if n else None,
            "fully_faithful": sum(1 for r in results if r["faithfulness"] == 1.0),
            "claims": len(claims),
            "unsupported_claims": sum(1 for c in claims if c["verdict"] == "unsupported"),
            "contradicted_claims": sum(1 for c in claims if c["verdict"] == "contradicted"),
            "addresses_question": sum(1 for r in results if r["addresses_question"])}


def render(summary: dict[str, Any], results: list[dict[str, Any]], source: str) -> str:
    mean = summary["mean_faithfulness"]
    lines = [f"# Judge results for `{source}` ({settings.JUDGE_MODEL}, effort {settings.JUDGE_EFFORT}, "
             f"{PROMPT_VERSION})", "",
             f"- Answers judged: {summary['answers']}; mean faithfulness: "
             f"{'n/a' if mean is None else f'{mean:.2f}'}; fully faithful: {summary['fully_faithful']}/"
             f"{summary['answers']}",
             f"- Claims: {summary['claims']} ({summary['unsupported_claims']} unsupported, "
             f"{summary['contradicted_claims']} contradicted)", "",
             f"## Hand audit ({min(AUDIT_ROWS, len(results))} verdicts; fill in 'human' with agree / disagree)", "",
             "| case | faithfulness | judge's unsupported/contradicted claims | human |", "|---|---|---|---|"]
    for r in results[:AUDIT_ROWS]:
        bad = "; ".join(f"{c['verdict']}: {c['claim']}" for c in r["claims"] if c["verdict"] != "supported")
        lines.append(f"| {r['id']} | {r['faithfulness']:.2f} | {(bad or '-').replace('|', '/')[:200]} |  |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Claim-level faithfulness judge over an eval report.")
    p.add_argument("report", type=Path)
    p.add_argument("--limit", type=int, default=0, help="judge at most N answers (0 = all)")
    p.add_argument("--cases", default="", help="comma-separated case ids")
    args = p.parse_args(argv)
    if settings.LLM_PROVIDER != "fake" and not settings.OPENAI_API_KEY:
        print("OPENAI_API_KEY is not set (the judge needs a real model)", file=sys.stderr)
        return 1
    with sync_session() as s:                       # fail before spending anything if the ledger is unreachable
        s.execute(text("SELECT 1"))
    report = json.loads(args.report.read_text(encoding="utf-8"))
    pairs = judgeable(report, [c for c in args.cases.split(",") if c])
    pairs = pairs[: args.limit] if args.limit else pairs
    owner_id = eval_user_id()
    results = []
    for case, run in pairs:
        results.append(judge_case(case, run, owner_id))
        print(f"  {case['id']}: faithfulness {results[-1]['faithfulness']:.2f}", file=sys.stderr)
    summary = summarize(results)
    out = {"source": str(args.report), "judge_model": settings.JUDGE_MODEL, "judge_effort": settings.JUDGE_EFFORT,
           "prompt_version": PROMPT_VERSION, "summary": summary, "results": results}
    base = args.report.parent / args.report.stem          # evals/reports/<timestamp>
    Path(f"{base}.judged.json").write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    markdown = render(summary, results, args.report.name)
    Path(f"{base}.judged.md").write_text(markdown, encoding="utf-8")
    print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
