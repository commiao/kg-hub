"""
kg-hub retrieval quality eval — Layer D of the quality framework.

Runs a fixed question set (tests/kg_eval.yaml) through the SAME search paths
real users hit (kg_search semantic edges + kg_episode_search fulltext) and
scores keyword recall. Answers the question the edge/entity ratio cannot:
"can the KG actually retrieve the things we know are in it?"

Read-only. No writes to FalkorDB or claude-mem.

Output:
  - stdout: per-question pass/fail + aggregate recall
  - file:   ~/.kg-hub/reports/kg-eval-YYYY-MM-DD.md
  - exit 0 if recall >= --min-recall (default 0.70), else 1

Usage:
  python -m tools.kg_eval                      # full eval, write report
  python -m tools.kg_eval --min-recall 0.8     # stricter gate
  python -m tools.kg_eval --no-write           # stdout only
  python -m tools.kg_eval --verbose            # show matched keywords + snippets
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from graphiti_client import build_graphiti  # noqa: E402

EVAL_PATH = Path(__file__).resolve().parent.parent / "tests" / "kg_eval.yaml"
REPORT_DIR = Path.home() / ".kg-hub" / "reports"


def now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


async def episode_fulltext(g, query: str, limit: int) -> list[str]:
    """Mirror kg_episode_search: FalkorDB fulltext over Episodic, fallback to substring."""
    driver = g.driver
    cypher = (
        "CALL db.idx.fulltext.queryNodes('Episodic', $q) YIELD node, score "
        "RETURN node.content AS content "
        "ORDER BY score DESC LIMIT " + str(limit)
    )
    try:
        rows, _, _ = await driver.execute_query(cypher, q=query)
    except Exception:
        rows, _, _ = await driver.execute_query(
            "MATCH (n:Episodic) WHERE n.content CONTAINS $q "
            "RETURN n.content AS content LIMIT " + str(limit),
            q=query,
        )
    out = []
    for r in rows:
        c = r.get("content") if isinstance(r, dict) else r["content"]
        if c:
            out.append(str(c))
    return out


async def run_question(g, item: dict, defaults: dict, verbose: bool) -> dict:
    q = item["q"]
    keywords = [k.lower() for k in item.get("keywords", [])]
    threshold = int(item.get("threshold", defaults.get("threshold", 1)))
    n_edges = int(item.get("num_edge_results", defaults.get("num_edge_results", 10)))
    n_eps = int(item.get("num_episode_results", defaults.get("num_episode_results", 5)))

    # 1. semantic edge search (kg_search path)
    edge_texts: list[str] = []
    try:
        edges = await g.search(query=q, num_results=min(n_edges, 30))
        edge_texts = [(e.fact or "") for e in edges]
    except Exception as exc:  # noqa: BLE001
        edge_texts = []
        if verbose:
            print(f"    [warn] edge search failed: {type(exc).__name__}: {exc}")

    # 2. episode fulltext (kg_episode_search path)
    ep_texts: list[str] = []
    try:
        ep_texts = await episode_fulltext(g, q, n_eps)
    except Exception as exc:  # noqa: BLE001
        if verbose:
            print(f"    [warn] episode search failed: {type(exc).__name__}: {exc}")

    haystack = " \n ".join(edge_texts + ep_texts).lower()
    matched = [k for k in keywords if k in haystack]
    passed = len(matched) >= threshold

    return {
        "id": item.get("id", q[:30]),
        "q": q,
        "keywords": keywords,
        "matched": matched,
        "threshold": threshold,
        "passed": passed,
        "n_edges": len(edge_texts),
        "n_episodes": len(ep_texts),
        "top_edge": edge_texts[0][:120] if edge_texts else "",
    }


def render(results: list[dict], min_recall: float) -> str:
    n = len(results)
    n_pass = sum(1 for r in results if r["passed"])
    recall = n_pass / n if n else 0.0
    gate_ok = recall >= min_recall

    L = []
    p = L.append
    p(f"# kg-hub Retrieval Eval — {now_iso()}")
    p("")
    p("Layer D quality check: runs the fixed question set through kg_search +")
    p("kg_episode_search and measures keyword recall. Read-only.")
    p("")
    p(f"## {'✅ PASS' if gate_ok else '🔴 FAIL'} — recall {recall:.0%} (gate ≥ {min_recall:.0%})")
    p("")
    p(f"- Questions: **{n}**")
    p(f"- Passed: **{n_pass}**")
    p(f"- Failed: **{n - n_pass}**")
    p("")
    p("## Per-question results")
    p("")
    p("| id | result | matched / needed | edges | eps | query |")
    p("|---|---|---|---|---|---|")
    for r in results:
        icon = "✓" if r["passed"] else "✗"
        mk = f"{len(r['matched'])}/{r['threshold']}"
        p(f"| {r['id']} | {icon} | {mk} | {r['n_edges']} | {r['n_episodes']} | {r['q'][:48]} |")
    p("")

    fails = [r for r in results if not r["passed"]]
    if fails:
        p("## Failures — retrieval gaps to investigate")
        p("")
        for r in fails:
            p(f"### ✗ {r['id']}")
            p(f"- query: `{r['q']}`")
            p(f"- expected any of: {r['keywords']}")
            p(f"- matched: {r['matched'] or '(none)'}")
            p(f"- search returned {r['n_edges']} edges, {r['n_episodes']} episodes")
            if r["top_edge"]:
                p(f"- top edge fact: _{r['top_edge']}_")
            p("")
        p("**Interpretation:** a failure means the KG either doesn't contain")
        p("this knowledge, or contains it but phrased so the search can't surface")
        p("it. Either way it's a real retrieval gap — not necessarily a bug, but")
        p("a known blind spot. Add the missing knowledge or adjust the keywords")
        p("if the question is poorly framed.")
        p("")

    p("---")
    p(f"Generated by `tools/kg_eval.py` from `tests/kg_eval.yaml`.")
    return "\n".join(L)


async def main_async(args) -> int:
    spec = yaml.safe_load(EVAL_PATH.read_text())
    defaults = spec.get("defaults", {})
    questions = spec.get("questions", [])
    if not questions:
        print("[eval] no questions in tests/kg_eval.yaml")
        return 1

    print(f"[eval] loading graphiti (FastEmbed model warm-up may take ~10s)...", file=sys.stderr)
    g = await build_graphiti(fresh=False)
    print(f"[eval] running {len(questions)} questions...", file=sys.stderr)

    results = []
    for i, item in enumerate(questions, 1):
        r = await run_question(g, item, defaults, args.verbose)
        results.append(r)
        icon = "✓" if r["passed"] else "✗"
        print(f"  [{i}/{len(questions)}] {icon} {r['id']:24s} "
              f"matched={len(r['matched'])}/{r['threshold']} "
              f"(edges={r['n_edges']}, eps={r['n_episodes']})", file=sys.stderr)
        if args.verbose and r["matched"]:
            print(f"        keywords hit: {r['matched']}", file=sys.stderr)

    md = render(results, args.min_recall)
    print()
    print(md)

    if not args.no_write:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        out = REPORT_DIR / f"kg-eval-{datetime.now().strftime('%Y-%m-%d')}.md"
        out.write_text(md)
        print(f"\n[report] {out}", file=sys.stderr)

    recall = sum(1 for r in results if r["passed"]) / len(results)
    return 0 if recall >= args.min_recall else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-recall", type=float, default=0.70,
                    help="recall gate for exit code (default 0.70)")
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
