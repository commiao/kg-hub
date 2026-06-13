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
import urllib.error
import urllib.parse
import urllib.request
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

# kg-hub server (NAS). The hook talks HTTP only — read + usage_count bump both
# happen server-side on localhost FalkorDB. This replaces the old direct
# cross-network FalkorDB connection, which after the NAS migration was too slow
# (~3.6s read) and silently dropped the 1s fail-fast usage bump.
KG_HUB_URL = (os.environ.get("KG_HUB_URL") or "http://127.0.0.1:8080").rstrip("/")
KG_HUB_API_TOKEN = os.environ.get("KG_HUB_API_TOKEN", "")
HTTP_TIMEOUT_SEC = 2.5  # per-call; well under the 5s SessionStart hook budget

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


def _connect(connect_timeout: float = 2.0, read_timeout: float = 2.0):
    """Open a FalkorDB connection. Returns (graph, None) or (None, errmsg).

    Timeouts are caller-tunable. The canonical-retrieval read path (which
    produces the chip) gets the default budget; the best-effort usage-count
    writeback passes a tighter, fail-fast timeout so it can never burn the hook
    budget when FalkorDB is busy. FalkorDB is single-threaded, so a concurrent
    heavy query stalls every other connection — including ours — and even the
    INFO/AUTH handshake inside the client constructor can time out."""
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
        # SessionStart hook has a hard wall-clock budget. Bound every redis
        # round-trip so a FalkorDB hiccup can't burn the whole timeout.
        # socket_connect_timeout: TCP handshake; socket_timeout: per-RPC.
        db = FalkorDB(
            host=host, port=port, password=pw,
            socket_connect_timeout=connect_timeout,
            socket_timeout=read_timeout,
        )
        graph = db.select_graph(os.environ.get("KG_HUB_FALKORDB_DATABASE", "kg_hub"))
        return graph, None
    except Exception as exc:
        return None, f"falkordb connect failed: {type(exc).__name__}: {exc}"


def _connect_read():
    """Connect for the chip read path, with escalating timeouts that ride out a
    cold FalkorDB.

    The real cause of dropped chips at session start is a *cold* FalkorDB: right
    after a Docker/launchd boot it accepts the TCP connection but is still
    loading its graph from disk, so the INFO/AUTH handshake (and any query)
    blocks until the load finishes. We can't probe for that LOADING state
    cheaply — the probe blocks too — so we escalate the timeout instead:

      1. Fast (2s): the warm, uncontended common case returns in ~10ms, so the
         normal path pays nothing.
      2. Cold-tolerant (4s): if the first attempt times out, assume the DB is
         loading (or briefly congested) and wait longer to ride out the tail.

    Only a *timeout* escalates; a hard error (auth, refused, import) won't fix
    itself. Worst case ≈ 2 + 0.4 + 4 = 6.4s, within the 10s hook budget; the
    usage bump runs after emit, so it never extends this path. FalkorDB itself
    parallelises queries across a thread pool (THREAD_COUNT = cores), so steady
    multi-client load does not stall this connect — only cold start does."""
    timeouts = (2.0, 4.0)
    last_err = None
    for i, t in enumerate(timeouts):
        graph, err = _connect(connect_timeout=t, read_timeout=t)
        if graph is not None:
            return graph, None
        last_err = err
        if "Timeout" not in (err or ""):
            break
        if i < len(timeouts) - 1:
            log(f"read connect timed out at {t:.0f}s; retrying cold-tolerant ({timeouts[i + 1]:.0f}s)")
            time.sleep(0.4)
    return None, last_err


def http_canonical_context(keyword: str, top_n: int, bump: bool) -> list[dict]:
    """Fetch ranked canonical/fulltext context for a keyword from the kg-hub
    server. The server runs the two-pass retrieval AND (when bump=True) bumps
    usage_count + last_used_at server-side on localhost FalkorDB — reliably,
    unlike the old cross-network 1s fail-fast write.

    Returns the server's already-ranked `picked` list (dicts with
    name/content/source/score), or [] on any failure (hook stays silent)."""
    qs = urllib.parse.urlencode({
        "kw": keyword,
        "top_n": top_n,
        "bump": "1" if bump else "0",
    })
    url = f"{KG_HUB_URL}/api/canonical_context?{qs}"
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {KG_HUB_API_TOKEN}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        picked = data.get("picked") or []
        if bump and picked:
            log(f"bumped usage on {data.get('bumped', 0)} episodes (server-side)")
        return picked
    except Exception as exc:
        log(f"http canonical_context failed for kw={keyword!r}: "
            f"{type(exc).__name__}: {exc}")
        return []


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
    graph, err = _connect_read()
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
    Returns how many got incremented.

    Runs AFTER the chip has been emitted, on a fail-fast (1s) connection.
    FalkorDB is single-threaded, so this write can stall behind a concurrent
    heavy query; a stall is expected and non-fatal, so we give up quietly
    rather than burn the hook budget or spam the log with an error."""
    if not names:
        return 0
    graph, err = _connect(connect_timeout=1.0, read_timeout=1.0)
    if graph is None:
        # A connect timeout here just means the DB was busy — not worth an error line.
        if "Timeout" not in err:
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
        if "Timeout" in type(exc).__name__:
            log("usage_count bump skipped (db busy)")
        else:
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


def build_chip(picked: list[dict], project_keyword: str, inj_chars: int) -> str:
    """Compact one-line visible status for the user (rendered as systemMessage).

    Truthful about mix: if all picked are canonical, says "N canonical pinned";
    if mixed, says "N pinned (X canonical)"; if none canonical, says "N obs pinned".

    Examples:
      📎 kg-hub: 3 canonical pinned (DESIGN, OBSERVATION-PHASE, ONBOARDING) · 1480 chars · cwd→kg-hub
      📎 kg-hub: 3 pinned (2 canonical) (DESIGN, obs-2734, obs-1039) · 1480 chars · cwd→workspace_cursor
      📎 kg-hub: 3 obs pinned (obs-2734, obs-1039, obs-1030) · 1480 chars · cwd→workspace_cursor
    """
    if not picked:
        return ""
    n = len(picked)
    short_names = []
    n_canonical = 0
    for p in picked:
        nm = p["name"]
        if nm.startswith("kg-hub-canonical-"):
            n_canonical += 1
            nm = nm[len("kg-hub-canonical-"):]
        elif nm.startswith("claude-mem-obs-"):
            nm = "obs-" + nm[len("claude-mem-obs-"):]
        short_names.append(nm)

    if n_canonical == n:
        label = f"{n} canonical pinned"
    elif n_canonical == 0:
        label = f"{n} obs pinned"
    else:
        label = f"{n} pinned ({n_canonical} canonical)"

    return (
        f"📎 kg-hub: {label} "
        f"({', '.join(short_names)}) · {inj_chars} chars · cwd→{project_keyword}"
    )


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


def write_cursor_rules_file(cwd: str, injection: str, chip: str) -> str | None:
    """Cursor does NOT consume hook-stdout `additionalContext` (its
    beforeSubmitPrompt adapter only honors {continue}). The real injection
    mechanism is a `.cursor/rules/*.mdc` file with `alwaysApply: true`
    frontmatter, which Cursor natively loads into every conversation in the
    workspace — this is exactly how claude-mem injects (see
    claude-mem-context.mdc).

    So for Cursor we WRITE the canonical content to
    `<cwd>/.cursor/rules/kg-hub-canonical.mdc` (separate file, never clobbers
    claude-mem's). The hook re-runs on every beforeSubmitPrompt, keeping it
    fresh. Returns the file path on success, None on failure.
    """
    try:
        rules_dir = Path(cwd) / ".cursor" / "rules"
        rules_dir.mkdir(parents=True, exist_ok=True)
        target = rules_dir / "kg-hub-canonical.mdc"
        # injection already carries the [SYSTEM: echo chip] preamble; the
        # frontmatter makes Cursor always-load it. Keep description short.
        content = (
            "---\n"
            "alwaysApply: true\n"
            'description: "kg-hub canonical context (auto-updated by PUSH hook)"\n'
            "---\n\n"
            f"{injection}\n"
        )
        target.write_text(content, encoding="utf-8")
        return str(target)
    except Exception as exc:
        log(f"cursor rules write failed: {type(exc).__name__}: {exc}")
        return None


def read_cwd_from_stdin_json(payload: dict) -> str | None:
    """For tools that pipe a JSON payload to stdin (Cursor / Codex), pull
    the workspace directory. Falls back through several common field names."""
    if not isinstance(payload, dict):
        return None
    # Cursor: workspace_roots is a list, cwd is sometimes also there
    wr = payload.get("workspace_roots")
    if isinstance(wr, list) and wr:
        return wr[0]
    # Codex / generic
    for k in ("cwd", "workspaceFolder", "working_directory", "project_dir"):
        if isinstance(payload.get(k), str) and payload[k]:
            return payload[k]
    return None


def read_stdin_payload(timeout_seconds: float = 1.0) -> dict:
    """Best-effort: read a JSON payload from stdin if one is present.
    Returns {} on any failure / no stdin / non-JSON. Bounded by select() so
    we never block the hook indefinitely."""
    import select
    try:
        if sys.stdin.isatty():
            return {}
        # Wait briefly for stdin to have data
        r, _, _ = select.select([sys.stdin], [], [], timeout_seconds)
        if not r:
            return {}
        data = sys.stdin.read()
        if not data.strip():
            return {}
        return json.loads(data)
    except Exception:
        return {}


def emit_for_format(fmt: str, chip: str, injection: str, picked: list, used_keyword: str) -> None:
    """Render the final hook response in the dialect of the target tool."""
    if fmt == "claude":
        # Claude Code SessionStart hook — current production behavior
        emit({
            "systemMessage": chip,
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": injection,
            },
        })
        return

    if fmt == "cursor":
        # Cursor's beforeSubmitPrompt adapter ONLY honors {continue} from hook
        # stdout — additionalContext is discarded (verified against claude-mem's
        # cursor adapter formatOutput, which returns only {continue:true}).
        # The actual injection happens by WRITING .cursor/rules/kg-hub-canonical.mdc
        # in main() before this call. Here we just tell Cursor to proceed.
        emit({"continue": True})
        return

    if fmt == "codex":
        # Codex CLI hook contract is not as well documented as Claude/Cursor.
        # Emit a permissive shape that covers the common patterns; tools that
        # don't read these keys treat the call as a no-op and the session
        # continues normally.
        emit({
            "continue": True,
            "context": injection,
            "additionalContext": injection,
            "message": chip,
        })
        return

    if fmt == "json":
        # Machine-readable: caller decides what to do with it.
        emit({
            "chip": chip,
            "injection": injection,
            "picked": [
                {"name": p["name"], "source": p["source"], "score": p["score"]}
                for p in picked
            ],
            "meta": {"keyword": used_keyword, "n_picked": len(picked)},
        })
        return

    if fmt == "text":
        # Plain markdown to stdout. Useful for piping into any prompt-builder.
        if chip:
            print(chip)
            print()
        print(injection)
        return

    # Unknown format — fall back to claude (least-surprise default)
    log(f"unknown --format {fmt!r}, falling back to claude")
    emit({
        "systemMessage": chip,
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": injection,
        },
    })


def empty_for_format(fmt: str) -> None:
    """Emit the no-op response in the requested dialect."""
    if fmt == "cursor":
        emit({"continue": True})
        return
    if fmt == "codex":
        emit({"continue": True})
        return
    if fmt == "json":
        emit({"chip": "", "injection": "", "picked": [], "meta": {}})
        return
    if fmt == "text":
        return
    # claude (default)
    emit(empty_output())


def main() -> int:
    t0 = time.time()
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="don't update usage_count")
    ap.add_argument("--probe", action="store_true",
                    help="dump match candidates for current dir, no JSON output")
    ap.add_argument(
        "--format", choices=["claude", "cursor", "codex", "json", "text"],
        default="claude",
        help="output dialect (default: claude — current Claude Code SessionStart hook)",
    )
    args = ap.parse_args()

    # For tools that pipe a JSON payload (Cursor, possibly Codex), read it.
    # For Claude Code, stdin is empty / tty and this returns {}.
    stdin_payload = read_stdin_payload() if args.format in ("cursor", "codex") else {}
    cwd_from_stdin = read_cwd_from_stdin_json(stdin_payload)

    cwd = (
        cwd_from_stdin
        or os.environ.get("CLAUDE_PROJECT_DIR")
        or os.environ.get("PWD")
        or os.getcwd()
    )
    keywords = derive_project_keywords(cwd)
    log(f"START fmt={args.format} cwd={cwd!r} keywords={keywords}")

    if args.probe:
        print(f"cwd={cwd}")
        print(f"keywords={keywords}")
        for kw in keywords:
            rows = http_canonical_context(kw, TOP_N, bump=False)
            print(f"\n--- keyword {kw!r}: {len(rows)} picked ---")
            for r in rows[:5]:
                print(f"  score={r['score']:.3f}  name={r['name']}  ({len(r['content'])} chars)")
        return 0

    # Try each keyword in priority order until we get hits. The server returns
    # the already-ranked picks AND bumps usage_count server-side (unless --dry),
    # so we no longer rank_and_pick or increment_usage on the client.
    picked = []
    used_keyword = None
    for kw in keywords:
        picked = http_canonical_context(kw, TOP_N, bump=(not args.dry))
        if picked:
            used_keyword = kw
            break

    if not picked:
        log(f"no match; elapsed={time.time()-t0:.2f}s")
        empty_for_format(args.format)
        return 0

    injection = build_injection(picked, used_keyword or "?")

    if not injection:
        log(f"picked but empty injection; elapsed={time.time()-t0:.2f}s")
        empty_for_format(args.format)
        return 0

    # Build the visible chip from the pre-preamble injection length so the
    # number the user sees matches the canonical payload (not the +preamble).
    inj_len = len(injection)
    chip = build_chip(picked, used_keyword or "?", inj_len)

    # stderr fallback — Claude Code renders SessionStart hook stderr inline
    # with a ⎿ prefix, which the systemMessage JSON field does not reliably do.
    if chip:
        print(chip, file=sys.stderr, flush=True)

    # L2 inline fallback — no agent runtime (Claude Code 2.1.x, Cursor, Codex)
    # reliably renders a SessionStart `systemMessage` / stderr line in its UI.
    # So the ONLY cross-tool way to give the user a visible "PUSH is alive +
    # here's what it referenced" signal is to ask the model to echo it.
    #
    # An HTML comment ("<!-- please echo -->") is too weak — Cursor's model
    # ignored it in testing. We use an explicit, imperative instruction block
    # that reads as a hard requirement, placed at the very top of the context
    # so it's the first thing the model processes. Kept to two lines so it
    # costs ~1 reply line and a trivial token slice.
    if chip and injection:
        l2_preamble = (
            "[SYSTEM: kg-hub auto-injected context below. Begin your very next "
            "reply with this exact line, verbatim, as a one-line acknowledgement "
            "so the user can see what was pinned — then answer normally:]\n"
            f"{chip}\n\n"
            "---\n\n"
        )
        injection = l2_preamble + injection

    # Cursor injection is via a rules file, not hook stdout. Write it before
    # emitting so the content is in place by the time Cursor proceeds.
    if args.format == "cursor" and not args.dry:
        mdc_path = write_cursor_rules_file(cwd, injection, chip)
        if mdc_path:
            log(f"wrote cursor rules file: {mdc_path}")

    # Emit the chip + injection FIRST. This is the time-critical output; do it
    # before the best-effort usage bump so FalkorDB contention can never delay
    # or suppress the chip. Format-aware dispatch — see emit_for_format().
    emit_for_format(args.format, chip, injection, picked, used_keyword or "?")
    sys.stdout.flush()

    elapsed = time.time() - t0
    # Log picked names + format so tools/push_status.py can report, per tool,
    # what the hook referenced and whether it's still firing (anti-black-box).
    picked_names = ",".join(p["name"] for p in picked)
    log(f"OK fmt={args.format} kw={used_keyword} picked={len(picked)} "
        f"names=[{picked_names}] inj_chars={inj_len} elapsed={elapsed:.2f}s")

    # NOTE: usage_count bump now happens server-side inside
    # http_canonical_context() (bump=1), reliably on localhost FalkorDB — no
    # client-side increment_usage() call needed. The legacy _connect /
    # fast_falkordb_query / increment_usage functions are retained only for
    # offline/debug use and are no longer on the hot path.
    return 0


if __name__ == "__main__":
    sys.exit(main())
