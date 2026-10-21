"""Write an eval report to disk and render its readable Markdown summary.

The JSON keeps everything (every answer, citation and retrieval rank) so numbers can be re-derived or judged later;
the Markdown is what docs/EVAL.md and the README quote. Every rate is printed with its raw count ("7/8 (88%)").
The hard set (cases tagged `hard`) and the --judge agreement get their own sections; reports written before they
existed simply don't have those keys."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def write_report(report: dict[str, Any], out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    json_path, md_path = out_dir / f"{stamp}.json", out_dir / f"{stamp}.md"
    json_path.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, md_path


def pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.0f}%"


def frac(stat: dict[str, Any] | None, ok: str = "ok") -> str:
    """{"ok": 7, "n": 8, "rate": 0.875} -> "7/8 (88%)"."""
    if not stat or not stat.get("n"):
        return "n/a"
    return f"{stat[ok]}/{stat['n']} ({pct(stat.get('rate'))})"


def ms(value: float | None) -> str:
    return "n/a" if value is None else f"{value:,.0f}"


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    return ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)] + ["| " + " | ".join(r) + " |"
                                                                             for r in rows]


def _hit(metrics: dict[str, Any], k: int) -> str:
    return frac(metrics.get(f"hit@{k}"), ok="hits")


def _mrr(metrics: dict[str, Any]) -> str:
    return "n/a" if metrics.get("mrr") is None else f"{metrics['mrr']:.3f}"


def render_meta(report: dict[str, Any]) -> list[str]:
    meta = report["meta"]
    models = (meta.get("health") or {}).get("models") or {}
    spend = meta.get("spend_usd") or {}
    dirty = " (dirty: uncommitted changes)" if meta.get("git_dirty") else ""
    lines = [f"# Vault eval report ({meta['started_at']})", "",
             f"- API: `{meta['api']}` | git `{meta.get('git_sha') or 'unknown'}`{dirty} | cases: {meta['cases']}"
             f" | tags: {', '.join(meta['tags']) or 'all'} | demo repeat: {meta['repeat']}",
             *_server_code(meta),
             f"- Server models (health): {', '.join(f'{k}={v}' for k, v in models.items()) or 'n/a'}"]
    server = (report.get("answers") or {}).get("server")
    if server:
        lines.append(f"- Answers served by: {_counts(server['models'])}; prompt versions: "
                     f"{_counts(server['prompt_versions'])}")
    if meta.get("skipped_cases"):
        lines.append(f"- Skipped (document not present): {', '.join(meta['skipped_cases'])}")
    if spend.get("delta") is not None:
        # the stack's bill for everything the run did (answers, condense, follow-ups, query embeddings), unlike the
        # answer-cost lines below, which cover one set of cases each
        covers = "every call, hard set included" if (report.get("answers") or {}).get("hard") else "every call"
        lines.append(f"- AI spend during the whole run (eval user, /metrics/ai-usage; {covers}): ${spend['delta']:.4f}")
    return lines + [""]


def _server_code(meta: dict[str, Any]) -> list[str]:
    """The API's code, as stated with --server-ref. Reports written before that flag (2026-10-21) have no such key
    and get no line."""
    if "server_ref" not in meta:
        return []
    if meta["server_ref"]:
        return [f"- Server code: `{meta['server_ref']}` (stated with --server-ref; `git` above is the eval client's "
                "checkout)"]
    return ["- Server code: not recorded (no --server-ref; `git` above is the eval client's checkout, not the API's)"]


def _counts(counter: dict[str, int]) -> str:
    return ", ".join(f"`{k}` x{v}" for k, v in counter.items()) or "n/a"


def _retrieval_table(summary: dict[str, Any], ks: list[int]) -> list[str]:
    header = ["mode", "cases"] + [f"hit@{k}" for k in ks] + ["MRR"]
    rows = [[mode, str(s["overall"]["n"])] + [_hit(s["overall"], k) for k in ks] + [_mrr(s["overall"])]
            for mode, s in summary.items()]
    return _table(header, rows)


def render_retrieval(retrieval: dict[str, Any]) -> list[str]:
    lines = ["## Retrieval (GET /search, no LLM)", ""]
    if "skipped" in retrieval:
        return lines + [f"Skipped: {retrieval['skipped']}.", ""]
    ks = retrieval["ks"]
    lines += _retrieval_table(retrieval["summary"], ks) + ["", "Per tag (hit@8 / MRR):", ""]
    modes = list(retrieval["summary"])
    tags = sorted({t for s in retrieval["summary"].values() for t in s["by_tag"]})
    tag_rows = []
    for tag in tags:
        cells = []
        for mode in modes:
            m = retrieval["summary"][mode]["by_tag"].get(tag)
            cells.append("n/a" if not m else f"{_hit(m, 8)} / {_mrr(m)}")
        tag_rows.append([tag] + cells)
    lines += _table(["tag"] + modes, tag_rows) + [""]
    if retrieval.get("hard"):
        lines += ["Hard set (tag `hard`; not in the tables above):", "", *_retrieval_table(retrieval["hard"], ks), ""]
    return lines


def render_answers(answers: dict[str, Any], retrieval_ran: bool = True) -> list[str]:
    lines = ["## Answers (POST /chat/sessions/{id}/messages)", ""]
    if "skipped" in answers:
        return lines + [f"Skipped: {answers['skipped']}.", ""]
    checks = answers["checks"]
    # The pass, check, by-tag, refusal and reliability numbers use each case's first run (so a demo case repeated 3x
    # weighs the same as any other); latency, cost and the demo table count every run, repeats included. With
    # --repeat the all-runs count shows whether a repeat failed (older reports don't have it).
    all_runs = answers.get("passed_all_runs")
    repeats = all_runs is not None and all_runs["n"] != answers["passed"]["n"]
    lines += [f"- First-run pass rate: **{frac(answers['passed'])}**"
              + (f" (all runs incl. demo repeats: {frac(all_runs)})" if repeats else ""),
              f"- not_found correct: {frac(checks['not_found'])} | citations valid (0 invalid ids): "
              f"{frac(checks['citations_valid'])} | gold citation: {frac(checks['gold_citation'])} | "
              f"grounded (0 ungrounded facts): {frac(checks['grounded'])}"]
    if answers["errors"]:
        lines.append(f"- Errors: {', '.join(answers['errors'])}")
    lines += ["", *_table(["tag", "passed"], [[t, frac(s)] for t, s in answers["by_tag"].items()]), ""]
    return lines + render_refusals(answers["refusals"]) + render_demo(answers["demo"]) + \
        render_reliability(answers["reliability"]) + render_latency_cost(answers, retrieval_ran)


def render_hard(hard: dict[str, Any] | None) -> list[str]:
    """The hard set's own summary: pass rate, checks and per tag. Short on purpose: its latency and cost are in the
    JSON, and its failures are in the Failures table with the others."""
    if not hard:
        return []
    n = hard["cases"]
    checks, refusals = hard["checks"], hard["refusals"]
    rows = [[t, frac(s)] for t, s in hard["by_tag"].items() if t != "hard"]
    return [f"## Hard set ({n} case{'' if n == 1 else 's'}, tag `hard`, not in the headline above)", "",
            f"- First-run pass rate (hard set): **{frac(hard['passed'])}**",
            f"- not_found correct: {frac(checks['not_found'])} | citations valid: {frac(checks['citations_valid'])} | "
            f"gold citation: {frac(checks['gold_citation'])} | grounded: {frac(checks['grounded'])}",
            f"- Refusals: {refusals['should_refuse']} should be refused, {refusals['fn']} answered instead, "
            f"{refusals['fp']} false refusals", "", *_table(["tag", "passed"], rows), ""]


def render_judge(judge: dict[str, Any] | None) -> list[str]:
    """Agreement of the LLM judge with the code's content checks, and every disagreement with the judge's reason."""
    if not judge:
        return []
    s = judge["summary"]
    sets = s["by_set"]
    by_set = " | ".join(f"{'original cases' if name == 'core' else 'hard set'} {v['agree']}/{v['judged']}"
                        for name, v in sets.items())
    stat = {"ok": s["agree"], "n": s["judged"], "rate": s["rate"]}
    lines = ["## LLM judge agreement (--judge, never gates)", "",
             "The judge grades each model-generated answer against the case's golden facts; it is compared with the "
             "code's content checks (any_of, all_of, forbid, forbid_start, not_found), not with citations or "
             "grounding, which it does not see.", "",
             f"- Agree: {frac(stat)}" + (f" | {by_set}" if by_set else ""),
             f"- Code pass, judge fail (candidate false passes): {', '.join(s['code_pass_judge_fail']) or 'none'}",
             f"- Code fail, judge pass (candidate false fails): {', '.join(s['code_fail_judge_pass']) or 'none'}",
             f"- Judge models: {_counts(s['models'])}"]
    if s["errors"]:
        lines.append(f"- Judge errors (not counted): {', '.join(s['errors'])}")
    rows = [[r["id"], r["set"], "pass" if r["code"] else "fail", "pass" if r["judge"] else "fail",
             ", ".join(r["failed"]) or "-", (r["rationale"] or "").replace("|", "/")[:200]]
            for r in judge["rows"] if r["judge"] is not None and r["judge"] != r["code"]]
    if rows:
        lines += ["", *_table(["case", "set", "code", "judge", "code's failed checks", "judge's reason"], rows)]
    return lines + [""]


def render_refusals(r: dict[str, Any]) -> list[str]:
    rows = [["all", str(r["refused"]), str(r["tp"]), str(r["fp"]), pct(r["precision"]), pct(r["recall"])]]
    for path, s in r["by_path"].items():
        rows.append([path, str(s["refusals"]), str(s["correct"]), str(s["false_refusals"]), pct(s["precision"]),
                     pct(s["recall"])])
    exp = r["expected_path"]
    return ["### Refusals", "", f"{r['should_refuse']} cases should be refused; {r['fn']} were answered instead.", "",
            *_table(["path", "refusals", "correct", "false refusals", "precision", "recall"], rows), "",
            f"Expected path (off-topic -> gate, in-domain -> model): {exp['matched']}/{exp['n']} "
            "(recorded, not failed on).", ""]


def render_demo(demo: list[dict[str, Any]]) -> list[str]:
    if not demo:
        return []
    rows = [[d["id"], f"{d['passes']}/{d['runs']}", "stable" if d["stable"] else ("FLAKY" if d["flaky"] else "FAIL")]
            for d in demo]
    flaky = [d["id"] for d in demo if d["flaky"]]
    return ["### Demo cases (must pass every run)", "", *_table(["case", "passes", "status"], rows), "",
            f"Flaky: {', '.join(flaky) if flaky else 'none'}", ""]


def render_reliability(rows: list[dict[str, Any]]) -> list[str]:
    body = [[r["confidence"], str(r["n"]), str(r["correct"]), pct(r["accuracy"])] for r in rows]
    return ["### Reliability (accuracy per confidence badge)", "",
            *_table(["confidence", "answers", "correct", "accuracy"], body), ""]


def ttft_cache_caveat(retrieval_ran: bool) -> str:
    """Printed under every latency table, because the numbers above it can't show it: the retrieval phase (when it
    runs; it needs /search) embeds the same questions first and the query-embedding cache keeps them 24 h, so an
    answer's own embedding call is usually a hit (during the 2026-10-14 full run all 105 embed_query calls were, per
    ai_usage.cache_hit). Without that phase only earlier runs can have warmed the cache."""
    warmed = ("mostly already cached: the retrieval phase above embeds the same questions, and so did earlier runs"
              if retrieval_ran else
              "possibly already cached by earlier runs of the same questions (this run had no retrieval phase)")
    return (f"TTFT here is measured with query embeddings {warmed} (cache TTL 24 h). A question the stack has never "
            "seen also pays the embedding call, about 0.5 s at the median (uncached `embed_query` rows in "
            "`ai_usage`).")


def render_latency_cost(answers: dict[str, Any], retrieval_ran: bool = True) -> list[str]:
    """TTFT is shown twice: over the answers the model generated (the number to quote) and over all answers, where
    gate refusals (~10 ms, no model call) pull the percentiles down. Older reports only have `ttft` (= all), and no
    `max`. Unlike the first-run rates above, these count every run, which the report says (n is runs, not cases)."""
    lat, cost = answers["latency_ms"], answers["cost_usd"]
    ttft = [("TTFT (server, all answers incl. gate refusals)", lat.get("ttft_all", lat["ttft"]))]
    if "ttft_model" in lat:
        ttft.insert(0, ("TTFT (server, model-generated answers only)", lat["ttft_model"]))
    rows = [[name, str(s["n"]), ms(s["p50"]), ms(s["p95"]), ms(s.get("max"))] for name, s in
            (*ttft, ("total (server)", lat["server_total"]), ("total (client)", lat["client_total"]))]
    per = "n/a" if cost["per_answer"] is None else f"${cost['per_answer']:.4f}"
    return ["### Latency and cost", "", "Latency and cost count every run, demo repeats included (n is runs, not "
            "cases).", "", *_table(["ms", "n", "p50", "p95", "max"], rows), "", ttft_cache_caveat(retrieval_ran), "",
            f"Answer-call cost: ${cost['total']:.4f} over {cost['answers']} answers ({per} per answer).", ""]


def rejected_phrases(run: dict[str, Any]) -> list[str]:
    """Phrases the answer contains but that didn't count, with the reason ("'are excluded' negated by 'not'"): the
    first thing to look at when a correct-looking answer failed. Reports before 2026-10-21 have no evidence."""
    return [f"'{e['phrase']}' {e['rejected']}" for items in (run.get("evidence") or {}).values() for e in items
            if e.get("rejected")]


def render_failures(report: dict[str, Any]) -> list[str]:
    rows = []
    for case in report["cases"]:
        for i, run in enumerate(case["runs"], start=1):
            if run["passed"]:
                continue
            answer = (run.get("answer") or {}).get("content") or run.get("error") or ""
            excerpt = " ".join(answer.split())[:140].replace("|", "/")
            why = rejected_phrases(run)
            failed = ", ".join(run["failed"]) + (f" ({'; '.join(why)})" if why else "")
            case_set = "hard" if "hard" in (case.get("tags") or []) else "core"
            rows.append([f"{case['id']} #{i}", case_set, failed.replace("|", "/"), excerpt])
    if not rows:
        return []
    return ["## Failures", "", *_table(["case", "set", "failed checks", "answer (start)"], rows), ""]


def render_markdown(report: dict[str, Any]) -> str:
    retrieval_ran = "skipped" not in report["retrieval"]
    lines = render_meta(report) + render_retrieval(report["retrieval"]) + \
        render_answers(report["answers"], retrieval_ran) + render_hard(report["answers"].get("hard")) + \
        render_judge(report.get("judge")) + render_failures(report)
    return "\n".join(lines).rstrip() + "\n"
