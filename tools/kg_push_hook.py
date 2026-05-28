"""
kg-hub PUSH hook for Claude Code SessionStart.

Runs at every session start, queries kg-hub for canonical content relevant to
the current project, and injects a short summary into the session's system
prompt via the `additionalContext` mechanism.

Design philosophy ("PUSH not PULL"): we don't rely on the agent to remember
to call `kg_search` — the platform-level hook always queries on the user's
behalf. This is what claude-mem already does with its flat observations;
this script adds the graph-canonical-content layer.

Hard constraints:
  * Total time < 4 s (Claude Code SessionStart hook timeout is 5 s)
  * Injection budget ≤ 1500 chars (~400 tokens) so it doesn't crowd out
    the user's prompt
  * Silent on failure — if anything goes wrong, output empty
    additionalContext so the session still starts cleanly
  * Only inject if there's a *meaningful* match — don't pollute unrelated
    sessions with kg-hub trivia
  * Increment usage_count on returned episodes — this produces the
    implicit-feedback signal needed for capsule-style ranking later

Reads from environment:
  * CLAUDE_PROJECT_DIR or PWD — used to derive a project keyword

Writes to:
  * stdout — JSON containing hookSpecificOutput / additionalContext
  * data/.push_hook.log — append-only debug log (small, last N lines kept)
  * Episodic.usage_count in FalkorDB — incremented for each returned ep

Usage:
  python -m tools.kg_push_hook         # standard hook mode
  python -m tools.kg_push_hook --dry   # print what would inject, no DB write
  python -m tools.kg_push_hook --probe # show match candidates for current dir
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Load FalkorDB credentials from ~/.claude-mem/.env (kg-hub convention).
# Best-effort: if dotenv is missing or .env is absent, we fall back to env vars.
try:
    from dotenv import load_dotenv
    load_dotenv(Path.home() / ".claude-mem" / ".env", override=False)
except Exception:
    pass

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = REPO_ROOT / "data" / ".push_hook.log"
LOG_MAX_LINES = 200    # keep log small

# Bounds
MAX_INJECTION_CHARS = 1500     # ~400 tokens
PER_EPISODE_EXCERPT = 400      # chars from each canonical episode
TOP_N = 3                      # most relevant episodes to inject


def log(line: str) -> None:
    """Append-only debug log with size cap."""
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with LOG_PATH.open("a") as f:
            f.write(f"{ts}  {line}\n")
        # rotate if too big
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > 50_000:
            lines = LOG_PATH.read_text().splitlines()[-LOG_MAX_LINES:]
            LOG_PATH.write_text("\n".join(lines) + "\n")
    except Exception:
        pass  # never let logging break the hook


_SYSTEM_DIRS = {
    "tmp", "var", "etc", "usr", "lib", "bin", "opt", "root", "home",
    "Users", "mac", "private", "sbin", "dev", "boot", "sys", "proc",
    "Applications", "Library", "System", "Volumes", "Network",
    "Desktop", "Documents", "Downloads", "Pictures", "Movies", "Music",
}

_MIN_KEYWORD_CHARS = 5


def derive_project_keywords(cwd: str) -> list[str]:
    """Convert cwd to a set of keywords for fulltext search.

    For /Users/mac/workspace_claudeCode/kg-hub →
        ['kg-hub', 'workspace_claudeCode']

    For /Users/mac/workspace_codex/foo →
        ['foo', 'workspace_codex']

    For /tmp, /Users, etc. → [] (skip system / generic paths so we don't
    spam unrelated sessions with canonical injection from substring matches
    like '/tmp/cache' appearing inside ROADMAP.md).

    The basename is usually the strongest signal; parent dir is a fallback.
    """
    p = Path(cwd).resolve()
    raw_parts = [p.name]
    if p.parent and p.parent.name:
        raw_parts.append(p.parent.name)

    keywords = []
    for kw in raw_parts:
        if not kw:
            continue
        if kw in _SYSTEM_DIRS:
            continue
        if len(kw) < _MIN_KEYWORD_CHARS:
            continue
        keywords.append(kw)
    return keywords


def empty_output() -> dict:
    """Return the no-op hook output (session starts as if hook didn't run)."""
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ""}}


def emit(output: dict) -> None:
    print(json.dumps(output, ensure_ascii=False))


def _connect():
    """Open a FalkorDB connection. Returns (graph, None) or (None, errmsg)."""
    try:
        from falkordb import FalkorDB
    except Exception as exc:
        return None, f"falkordb import failed: {type(exc).__name__}: {exc}"
    try:
        host = os.environ.get("KG_HUB_FALKORDB_HOST", "127.0.0.1")
        # macOS sometimes loses 'localhost' resolution under launchd / hooks; force 127.0.0.1
        if host == "localhost":
            host = "127.0.0.1"
        port = int(os.environ.get("KG_HUB_FALKORDB_PORT", "6379"))
        pw = os.environ.get("KG_HUB_FALKORDB_PASSWORD") or None
        db = FalkorDB(host=host, port=port, password=pw)
        graph = db.select_graph(os.environ.get("KG_HUB_FALKORDB_DATABASE", "kg_hub"))
        return graph, None
    except Exception as exc:
        return None, f"falkordb connect failed: {type(exc).__name__}: {exc}"


def fast_falkordb_query(keyword: str, top_n: int) -> list[dict]:
    """Two-pass retrieval to compensate for BM25-like score dilution on long
    canonical docs:

      Pass 1 (canonical-first, substring): scan kg-hub-canonical-* nodes
        whose content contains the keyword. These get a forced high score
        (canonical content is curated and high-trust).
      Pass 2 (general fulltext): standard fulltext over all Episodic nodes,
        used to fill the result set when canonical didn't yield enough.

    Returns list of dicts with name/content/source/score. Empty on failure.
    """
    graph, err = _connect()
    if graph is None:
        log(err)
        return []

    rows: list[dict] = []
    seen_names: set[str] = set()

    # Pass 1: canonical CONTAINS — only 5 nodes max, very cheap
    try:
        result = graph.query(
            "MATCH (n:Episodic) WHERE n.name STARTS WITH 'kg-hub-canonical' "
            "AND n.content CONTAINS $kw "
            "RETURN n.name AS name, n.content AS content, "
            "n.source_description AS source",
            params={"kw": keyword},
        )
        for r in result.result_set:
            rows.append({
                "name": r[0], "content": r[1] or "", "source": r[2] or "",
                "score": 100.0,  # forced priority; canonical beats fulltext score
            })
            seen_names.add(r[0])
    except Exception as exc:
        log(f"canonical pass failed: {type(exc).__name__}: {exc}")

    # Pass 2: general fulltext to fill remaining slots
    if len(rows) < top_n:
        try:
            result = graph.query(
                "CALL db.idx.fulltext.queryNodes('Episodic', $q) YIELD node, score "
                "WHERE NOT node.name IN $exclude "
                "RETURN node.name AS name, node.content AS content, "
                "node.source_description AS source, score "
                "ORDER BY score DESC LIMIT $lim",
                params={
                    "q": keyword,
                    "exclude": list(seen_names),
                    "lim": (top_n - len(rows)) * 3,
                },
            )
            for r in result.result_set:
                rows.append({
                    "name": r[0], "content": r[1] or "", "source": r[2] or "",
                    "score": float(r[3] or 0),
                })
        except Exception as exc:
            log(f"fulltext pass failed for q={keyword!r}: {type(exc).__name__}: {exc}")

    return rows


def increment_usage(names: list[str]) -> int:
    """Best-effort: bump usage_count on the returned episodes.
    Returns how many got incremented."""
    if not names:
        return 0
    graph, err = _connect()
    if graph is None:
        log(err)
        return 0
    try:
        result = graph.query(
            "MATCH (n:Episodic) WHERE n.name IN $names "
            "SET n.usage_count = coalesce(n.usage_count, 0) + 1, "
            "    n.last_used_at = $now "
            "RETURN count(n)",
            params={"names": names, "now": datetime.now(tz=timezone.utc).isoformat()},
        )
        if result.result_set:
            return int(result.result_set[0][0])
    except Exception as exc:
        log(f"usage_count update failed: {type(exc).__name__}: {exc}")
    return 0


def rank_and_pick(rows: list[dict], top_n: int) -> list[dict]:
    """Prefer canonical episodes; among those, prefer higher score.
    Then fill with other Episodic up to top_n."""
    canonical = [r for r in rows if r["name"].startswith("kg-hub-canonical-")]
    others = [r for r in rows if not r["name"].startswith("kg-hub-canonical-")]
    canonical.sort(key=lambda r: -r["score"])
    others.sort(key=lambda r: -r["score"])
    picked = canonical[:top_n]
    if len(picked) < top_n:
        picked.extend(others[: top_n - len(picked)])
    return picked


def build_injection(picked: list[dict], project_keyword: str) -> str:
    """Format the chosen episodes into a compact markdown injection."""
    if not picked:
        return ""

    parts = [
        "## kg-hub canonical context (auto-injected at SessionStart)",
        "",
        f"Based on `cwd → {project_keyword}`, the following may be relevant.",
        "Pinned via PUSH hook — query the graph directly for full content.",
        "",
    ]
    for r in picked:
        body = (r["content"] or "").strip()
        # Take the first PER_EPISODE_EXCERPT chars, end on a sentence/line boundary if possible
        excerpt = body[:PER_EPISODE_EXCERPT]
        if len(body) > PER_EPISODE_EXCERPT:
            # try to cut at last newline
            cut = excerpt.rfind("\n")
            if cut > PER_EPISODE_EXCERPT * 0.6:
                excerpt = excerpt[:cut]
            excerpt = excerpt.rstrip() + "\n[…truncated; full episode in graph]"
        parts.append(f"### {r['name']}")
        parts.append(f"_source: {r['source']}, fulltext score: {r['score']:.3f}_")
        parts.append("")
        parts.append(excerpt)
        parts.append("")

    out = "\n".join(parts)
    if len(out) > MAX_INJECTION_CHARS:
        out = out[:MAX_INJECTION_CHARS - 50].rstrip() + "\n\n[…injection truncated to budget]"
    return out


def main() -> int:
    t0 = time.time()
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="don't update usage_count")
    ap.add_argument("--probe", action="store_true",
                    help="dump match candidates for current dir, no JSON output")
    args = ap.parse_args()

    cwd = os.environ.get("CLAUDE_PROJECT_DIR") or os.environ.get("PWD") or os.getcwd()
    keywords = derive_project_keywords(cwd)
    log(f"START cwd={cwd!r} keywords={keywords}")

    if args.probe:
        print(f"cwd={cwd}")
        print(f"keywords={keywords}")
        for kw in keywords:
            rows = fast_falkordb_query(kw, TOP_N)
            print(f"\n--- keyword {kw!r}: {len(rows)} candidates ---")
            for r in rows[:5]:
                print(f"  score={r['score']:.3f}  name={r['name']}  ({len(r['content'])} chars)")
        return 0

    # Try each keyword in priority order until we get hits
    rows = []
    used_keyword = None
    for kw in keywords:
        rows = fast_falkordb_query(kw, TOP_N)
        if rows:
            used_keyword = kw
            break

    if not rows:
        log(f"no match; elapsed={time.time()-t0:.2f}s")
        emit(empty_output())
        return 0

    picked = rank_and_pick(rows, TOP_N)
    injection = build_injection(picked, used_keyword or "?")

    if not injection:
        log(f"picked but empty injection; elapsed={time.time()-t0:.2f}s")
        emit(empty_output())
        return 0

    # Implicit-feedback signal: bump usage on the picked episodes
    if not args.dry:
        bumped = increment_usage([p["name"] for p in picked])
        log(f"bumped usage on {bumped} episodes")

    elapsed = time.time() - t0
    log(f"OK kw={used_keyword} picked={len(picked)} inj_chars={len(injection)} elapsed={elapsed:.2f}s")

    emit({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": injection,
        }
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
