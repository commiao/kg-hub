"""tools/capsules.py — 实时查看知识胶囊清单与排序（只读）。

随时运行，查 NAS 上的实时数据：
  1. 全部 canonical 胶囊总览：scope / 曝光(usage_count) / 最近使用。
  2. 在指定 cwd 关键词下的实时排序：每张胶囊的相关性 score，以及哪 top_n 会被注入。

只读：bump=0，不污染 usage_count。

用法：
  python -m tools.capsules                         # 总览 + 当前 cwd 关键词的排序
  python -m tools.capsules --kw kg-hub             # 指定关键词
  python -m tools.capsules --kw workspace_claudeCode --top 3
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path.home() / ".claude-mem" / ".env", override=False)
except Exception:
    pass

# 与 kg_hub_server.CANONICAL_SCOPE 镜像（服务端不在 API 里回 scope，这里仅供展示）。
SCOPE = {
    "kg-hub-canonical-DESIGN": "project:kg-hub",
    "kg-hub-canonical-ROADMAP": "project:kg-hub",
    "kg-hub-canonical-PHASE-3-REPORT": "project:kg-hub",
    "kg-hub-canonical-OBSERVATION-PHASE": "project:kg-hub",
    "kg-hub-canonical-INCIDENT-RETRO": "project:kg-hub",
    "kg-hub-canonical-NOTIFICATION": "project:kg-hub",
    "kg-hub-canonical-ONBOARDING": "global",
    "kg-hub-canonical-INTEGRATION-GUIDE": "global",
    "kg-hub-canonical-AGENT-TOOL-DISCOVERY": "global",
}


def _get(path: str, timeouts=(8, 20, 30)) -> dict:
    base = (os.environ.get("KG_HUB_URL") or "http://127.0.0.1:8080").rstrip("/")
    tok = os.environ.get("KG_HUB_API_TOKEN") or ""
    req = urllib.request.Request(
        f"{base}{path}", headers={"Authorization": f"Bearer {tok}"} if tok else {})
    last = None
    for i, t in enumerate(timeouts):
        try:
            with urllib.request.urlopen(req, timeout=t) as r:
                return json.loads(r.read())
        except Exception as exc:
            last = exc
            if i < len(timeouts) - 1:
                time.sleep(1.0)
    raise last


def short(n: str) -> str:
    return n.replace("kg-hub-canonical-", "")


def derive_kw() -> str:
    p = Path(os.getcwd()).name
    return p if len(p) >= 5 else "kg-hub"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kw", default=None, help="cwd 关键词（默认取当前目录名）")
    ap.add_argument("--top", type=int, default=3, help="注入名额 top_n（标 📌）")
    args = ap.parse_args()
    kw = args.kw or derive_kw()

    try:
        ur = _get("/api/usage_ranking?top_n=50")
        cc = _get(f"/api/canonical_context?kw={urllib.parse.quote(kw)}&top_n=20&bump=0")
    except Exception as exc:
        print(f"[capsules] 连不上 kg-hub：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    usage, last_used = {}, {}
    for x in ur.get("top_canonical", []):
        usage[x["name"]] = x["usage_count"]
        last_used[x["name"]] = (x.get("last_used_at") or "")[:10]
    for x in ur.get("demote", []):
        usage.setdefault(x["name"], 0)
        last_used.setdefault(x["name"], "—")
    st = ur.get("stats", {})

    # ---- 1. 总览 ----
    print(f"# kg-hub 知识胶囊总览（实时）")
    if st:
        print(f"胶囊 {st.get('canonical_total','?')} 个 · 累计注入 "
              f"{st.get('canonical_total_usage','?')} 次 · 全图 episode {st.get('total_episodes','?')}")
    print()
    print("| 胶囊 | scope | 曝光(usage) | 最近使用 |")
    print("|---|---|---|---|")
    for name in sorted(usage, key=lambda n: -usage[n]):
        print(f"| {short(name)} | {SCOPE.get(name,'?')} | {usage[name]} | {last_used.get(name,'—')} |")

    # ---- 2. 指定关键词下的实时排序 ----
    picked = [x for x in cc.get("picked", []) if x["name"].startswith("kg-hub-canonical-")]
    picked.sort(key=lambda x: -x["score"])
    print()
    print(f"## 在 cwd 关键词 `{kw}` 下的实时排序（📌 = 进 top-{args.top} 会被注入）")
    print()
    print("| 排名 | 胶囊 | score | scope | 注入? |")
    print("|---|---|---|---|---|")
    if not picked:
        print("| — | （该关键词下无合格胶囊） | | | |")
    for i, x in enumerate(picked, 1):
        inj = "📌" if i <= args.top else ""
        print(f"| {i} | {short(x['name'])} | {x['score']:.3f} | {SCOPE.get(x['name'],'?')} | {inj} |")
    print()
    print("说明：score = log1p(关键词命中数) + scope 加成（本项目 +0.5 / 别的项目 -0.3 / global 0）；"
          "排序+探索槽见 kg_hub_server.canonical_context。曝光=被注入次数（非贡献度）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
