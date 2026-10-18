"""Explain one retrieval: call GET /api/v1/search and print the RRF arithmetic behind every hit.

    uv run python scripts/explain_retrieval.py --key vault-demo-key "What is the notice period in clause 9.2?"
    uv run python scripts/explain_retrieval.py --key vault-demo-key --mode keyword "LRK-4471" <document_id> ...

For each hit it shows the vector rank, the keyword rank and how they add up:
    rrf = 1/(RRF_K + sem_rank) + LEXICAL_WEIGHT * 1/(RRF_K + lex_rank)      (a missing rank contributes 0)
          + 1/(RRF_K + 1) when the hit matched an exact identifier          (the identifier boost, D-02)
and checks the recomputed value against the one the API returned. RRF_K / LEXICAL_WEIGHT / ID_HIT_BOOST default to
this checkout's settings; pass --rrf-k / --w-lex / --no-id-boost if the server runs with different values.
"""

import argparse
import sys
import textwrap
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.rag import retrieval  # noqa: E402


def leg(rank: int | None, k: int, weight: float = 1.0) -> tuple[float, str]:
    if rank is None:
        return 0.0, "0"
    value = weight * 1 / (k + rank)
    prefix = "" if weight == 1.0 else f"{weight:g} x "
    return value, f"{prefix}1/({k}+{rank})"


def boost_leg(id_hit: bool, k: int, enabled: bool) -> tuple[float, str]:
    """retrieve()'s identifier boost (D-02), from the same function (app/rag/retrieval.py id_boost): a chunk that
    matched an exact identifier also scores as a rank-1 vector hit. Without it every identifier hit would print
    MISMATCH."""
    if not (id_hit and enabled):
        return 0.0, "0"
    return retrieval.id_boost(k, enabled), f"1/({k}+1)"


def legs(hit: dict, rrf_k: int, w_lex: float, id_boost: bool) -> list[tuple[float, str]]:
    """The terms of retrieve()'s fused score for one hit: vector leg, keyword leg, identifier boost."""
    return [leg(hit["sem_rank"], rrf_k), leg(hit["lex_rank"], rrf_k, w_lex), boost_leg(hit["id_hit"], rrf_k, id_boost)]


def fused_score(hit: dict, rrf_k: int, w_lex: float, id_boost: bool) -> float:
    return sum(value for value, _ in legs(hit, rrf_k, w_lex, id_boost))


def explain_hit(i: int, hit: dict, rrf_k: int, w_lex: float, id_boost: bool = settings.ID_HIT_BOOST) -> str:
    sem, lex, boost = legs(hit, rrf_k, w_lex, id_boost)
    terms = [sem, lex, boost] if boost[0] else [sem, lex]          # show the boost only on hits it applies to
    total = fused_score(hit, rrf_k, w_lex, id_boost)
    check = "ok" if abs(total - hit["rrf"]) < 1e-5 else f"MISMATCH (api {hit['rrf']:.6f})"
    sim = "-" if hit["similarity"] is None else f"{hit['similarity']:.3f}"
    lines = [
        f"#{i}  {hit['filename']} · {hit['label']} · chunk {hit['chunk_id']}"
        + ("  [identifier match]" if hit["id_hit"] else ""),
        f"    vector rank {hit['sem_rank'] or '-':>3}  (similarity {sim})   keyword rank {hit['lex_rank'] or '-':>3}",
        f"    rrf = {' + '.join(txt for _, txt in terms)} = {' + '.join(f'{v:.5f}' for v, _ in terms)}"
        f" = {total:.5f}   {check}",
        textwrap.indent(textwrap.fill(hit["snippet"], width=100), "    > "),
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("question")
    parser.add_argument("document_ids", nargs="*", help="limit to these documents (default: all ready documents)")
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--key", default=settings.SEED_DEMO_API_KEY)
    parser.add_argument("--mode", choices=["hybrid", "vector", "keyword"], default="hybrid")
    parser.add_argument("--k", type=int, default=settings.RETRIEVAL_K)
    parser.add_argument("--rrf-k", type=int, default=settings.RRF_K)
    parser.add_argument("--w-lex", type=float, default=settings.LEXICAL_WEIGHT)
    parser.add_argument("--id-boost", action=argparse.BooleanOptionalAction, default=settings.ID_HIT_BOOST)
    args = parser.parse_args()

    params: dict = {"q": args.question, "mode": args.mode, "k": args.k}
    if args.document_ids:
        params["document_ids"] = args.document_ids
    r = httpx.get(f"{args.api}/api/v1/search", params=params, headers={"X-API-Key": args.key}, timeout=30)
    if r.status_code != 200:
        print(f"HTTP {r.status_code}: {r.text}")
        return 1
    body = r.json()
    top = "-" if body["top_similarity"] is None else f"{body['top_similarity']:.3f}"
    print(f"query        : {body['query']}")
    print(f"mode         : {body['mode']}   (requested {args.mode})")
    print(f"terms        : {' | '.join(body['terms']) or '(none: vector leg only)'}")
    print(f"identifiers  : {', '.join(body['id_terms']) or '(none)'}   -> ranked first in the keyword leg")
    print(f"top similarity {top}   gate fires below {settings.GATE_MIN_SIMILARITY} unless an identifier matched")
    boost = f" + 1/({args.rrf_k} + 1) on an identifier match" if args.id_boost else ""
    print(f"formula      : rrf = 1/({args.rrf_k} + sem_rank) + {args.w_lex:g} x 1/({args.rrf_k} + lex_rank){boost}\n")
    if not body["hits"]:
        print("no hits")
    for i, hit in enumerate(body["hits"], start=1):
        print(explain_hit(i, hit, args.rrf_k, args.w_lex, args.id_boost) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
