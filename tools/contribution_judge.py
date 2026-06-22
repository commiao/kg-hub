"""tools/contribution_judge.py — Tier 2 contribution signal: LLM-judged usefulness.

Tier 1 (engagement_audit.py) is deterministic term-overlap: cheap but only correlation,
and blind when a session paraphrases a capsule instead of reusing its exact identifiers
(that's why DESIGN scored 0% — design vocabulary rarely reappears verbatim in code work).

Tier 2 closes that gap with an LLM judge (human OUT of the loop): for each (injected
capsule × session it started), ask a cheap model whether the capsule actually informed
what the session did. Score 0/1/2 with REQUIRED evidence; default 0 without it. See
docs/CONTRIBUTION-SIGNAL.md.

Reuses Tier 1's injection↔session join (data/.push_hook.log + claude-mem.db). Same
scope caveat: this machine's Claude Code sessions only; still a proxy, not causal truth
(Tier 3 ablation is the calibrator). Read-only — reports, does not write the graph yet.

LLM: the project's 百炼-proxied Anthropic endpoint (ANTHROPIC_* in ~/.claude-mem/.env).
qwen3.6-plus runs in thinking mode, which forbids forced tool_choice → we ask for plain
JSON and parse it, and inject thinking={"type":"disabled"} like graphiti_client.build_llm.
Calls are throttled (百calls quota ~20/min) and sequential.

Usage:
  python -m tools.contribution_judge                 # judge all matched pairs
  python -m tools.contribution_judge --limit 6       # cheap smoke test
  python -m tools.contribution_judge --verbose       # print per-pair evidence
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path.home() / ".claude-mem" / ".env", override=False)
except Exception:
    pass

from tools.engagement_audit import (  # reuse the Tier-1 join
    REPORTS, fetch_capsule_texts, fetch_usage, load_injections, load_sessions,
    match_session, parse_ts,
)

CAP_EXCERPT = 1500       # chars of capsule fed to the judge
SESS_EXCERPT = 2500      # chars of session activity fed to the judge
MIN_INTERVAL = float(os.environ.get("KG_HUB_LLM_MIN_INTERVAL_SEC", "4.0"))

PROMPT = """你在评估一段被自动注入到会话开头的「知识胶囊」对该会话**实际工作**的真实贡献。

严格规则（违反则判 0）：
- **同一话题 ≠ 有贡献**。只有胶囊里的**具体内容**（结论/方案/标识符/步骤/数值）被会话用于其工作，才算。
- 不接受元描述当证据（如「这是常被注入的文档」「在某关键词下被选中」）——那是曝光，不是贡献。
- 若该信息本可从用户与助手的对话本身得到，不能记到胶囊头上。
- 证据必须把「胶囊里的具体点」和「会话里的对应动作」一一对上；对不上就判 0。

评分：0 = 没用到 / 仅话题相同；1 = 具体内容提供了背景但非直接依据；2 = 具体内容被直接用于决策/代码/排查/答案。

只输出一行 JSON，不要解释：
{{"score":0,"used":false,"evidence":"<=40字，引用胶囊具体点↔会话动作的对应；无则留空>"}}

== 胶囊：{name} ==
{capsule}

== 这个会话实际做了什么（observations 摘要）==
{session}
"""

_JSON_RE = re.compile(r"\{.*?\}", re.S)


def build_client():
    from anthropic import AsyncAnthropic
    client = AsyncAnthropic(
        auth_token=os.environ["ANTHROPIC_AUTH_TOKEN"],
        base_url=os.environ["ANTHROPIC_BASE_URL"],
        max_retries=4, timeout=120.0,
    )
    return client


async def judge_one(client, model, name, capsule, session) -> dict:
    prompt = PROMPT.format(
        name=name.replace("kg-hub-canonical-", ""),
        capsule=capsule[:CAP_EXCERPT],
        session=session[:SESS_EXCERPT] or "(本会话无可用 observations 文本)",
    )
    try:
        resp = await client.messages.create(
            model=model, max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
            extra_body={"thinking": {"type": "disabled"}},
        )
        text = "".join(getattr(b, "text", "") for b in resp.content)
        m = _JSON_RE.search(text)
        if not m:
            return {"score": 0, "used": False, "evidence": "", "_err": "no-json"}
        d = json.loads(m.group(0))
        d["score"] = max(0, min(2, int(d.get("score", 0))))
        d.setdefault("evidence", "")
        return d
    except Exception as exc:
        return {"score": 0, "used": False, "evidence": "", "_err": f"{type(exc).__name__}: {exc}"}


async def run(args) -> int:
    since_epoch = parse_ts(args.since + "T00:00:00Z")
    injections = load_injections(since_epoch)
    by_proj = load_sessions()
    try:
        cap_text = fetch_capsule_texts()
        usage = fetch_usage()
    except Exception as exc:
        print(f"[contribution_judge] cannot reach kg-hub server: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 1

    # Dedup (capsule, session) pairs across injections.
    pairs: dict[tuple, dict] = {}
    for inj in injections:
        sess = match_session(inj, by_proj)
        if not sess:
            continue
        for name in inj["names"]:
            if name not in cap_text:
                continue
            pairs.setdefault((name, sess["sid"]), {"name": name, "sess": sess})
    pair_list = list(pairs.values())
    if args.limit:
        pair_list = pair_list[: args.limit]
    if not pair_list:
        print("[contribution_judge] no matched (capsule, session) pairs to judge.", file=sys.stderr)
        return 1

    model = os.environ.get("ANTHROPIC_DEFAULT_HAIKU_MODEL") or os.environ.get("ANTHROPIC_MODEL", "qwen3.6-plus")
    print(f"[contribution_judge] judging {len(pair_list)} pairs with {model} "
          f"(~{MIN_INTERVAL:.0f}s apart)…", file=sys.stderr)
    client = build_client()

    stat = defaultdict(lambda: {"pairs": 0, "sum": 0, "ge1": 0, "eq2": 0})
    for i, p in enumerate(pair_list):
        if i and MIN_INTERVAL > 0:
            await asyncio.sleep(MIN_INTERVAL)
        v = await judge_one(client, model, p["name"], cap_text[p["name"]], p["sess"]["text"])
        st = stat[p["name"]]
        st["pairs"] += 1
        st["sum"] += v["score"]
        st["ge1"] += 1 if v["score"] >= 1 else 0
        st["eq2"] += 1 if v["score"] == 2 else 0
        if args.verbose:
            tag = f" ERR={v['_err']}" if v.get("_err") else ""
            print(f"  [{i+1}/{len(pair_list)}] {p['name'].replace('kg-hub-canonical-','')} "
                  f"sess={p['sess']['sid'][:8]} score={v['score']} ev={v.get('evidence','')!r}{tag}",
                  file=sys.stderr)

    L = [
        f"# kg-hub 胶囊 贡献度 (Tier 2, LLM 裁判) — {datetime.now(tz=timezone.utc).date()}",
        f"窗口 since {args.since} · 模型 {model} · 本机 Claude Code only · 代理信号(非因果)",
        "",
        "| 胶囊 | usage(曝光) | 评判对数 | 贡献分均值 | ≥1(有用) | =2(直接用) |",
        "|---|---|---|---|---|---|",
    ]
    names = sorted(stat, key=lambda n: -(stat[n]["sum"] / max(stat[n]["pairs"], 1)))
    for name in names:
        st = stat[name]
        avg = st["sum"] / st["pairs"] if st["pairs"] else 0
        L.append(f"| {name.replace('kg-hub-canonical-','')} | {usage.get(name,0)} | "
                 f"{st['pairs']} | {avg:.2f} | {st['ge1']} | {st['eq2']} |")
    L += [
        "",
        "- **贡献分均值**：0=没用到 / 1=相关背景 / 2=直接用到，对该胶囊所有评判会话取平均。",
        "- 曝光高但均值低 = 钉得多没真贡献；均值高 = 真在帮上忙。",
        "- 对照 Tier 1 `engagement_audit.py`（确定性术语重合）交叉验证；分歧处看 evidence。",
        "- 仍是代理：Tier 3 消融才是因果真值。结果未写回图（排序仍用相关性+探索）。",
    ]
    md = "\n".join(L)
    print(md)
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / f"contribution-{datetime.now().strftime('%Y-%m-%d')}.md").write_text(md)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-06-16")
    ap.add_argument("--limit", type=int, default=0, help="cap number of pairs judged (0 = all)")
    ap.add_argument("--verbose", action="store_true", help="print per-pair score + evidence")
    args = ap.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
