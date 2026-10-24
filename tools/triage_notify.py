"""观测分诊的摘要与飞书推送：新观测过滤的效果、高 / 低价值保留率、这一阶段省了多少 token。

为什么要有它：分诊（utils/obs_triage）的计数只写在战役批次文件里，影子模式跑着却没人看得到（用户 2026-10-24）。
这里把三件用户关心的事放进一条消息：
  1. 过滤效果：领批时闸拒了多少、分诊判丢 / 判复核多少（战役批次文件，tools/extract_campaign.triage_summary）
  2. 保留率：高价值保留率、低价值保留率——没有逐条标签，取冻结样本上当前规则版本最新一次评分（tools/obs_bench score）
  3. token：判丢条数 × 该模型本战役自报的 token/条。影子模式下是「启用就能省」，启用后是「已省」

什么时候推：
  - 战役打满（每个模型领满配额、且没有没提交的批）——extract_campaign submit 正式提交后顺手检查，每条战役只推一次
  - 每周日 09:40（launchd com.kg-hub.triage-digest）：过去 7 天有领批的战役各一段；顺带补推漏掉的打满通知
飞书走 feishu-notify 的 kg-hub webhook（与 feedback_digest / capsule_watch 同一条）；推送失败只记 stderr，不影响提交。

用法：
  python -m tools.triage_notify digest --id <战役> [--dry]     # 看 / 推一条战役的摘要
  python -m tools.triage_notify weekly [--dry]                 # 每周摘要（launchd 跑这个）
  python -m tools.triage_notify check-full [--dry]             # 扫一遍打满还没推过的战役
  python -m tools.triage_notify sample --id <战役> [--tier drop] [--n 20]   # 从判丢档抽样复查（打印 id 与标题）
"""
from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import extract_campaign as ec  # noqa: E402
from tools import obs_bench  # noqa: E402
from utils import obs_triage  # noqa: E402

FEISHU_SEND = Path.home() / ".claude" / "skills" / "feishu-notify" / "scripts" / "send.py"
TITLE = "kg-hub 观测分诊"


def state_path() -> Path:
    return ec.ax.state_dir() / "triage-notify.json"


def load_state() -> dict:
    p = state_path()
    return json.loads(p.read_text()) if p.exists() else {"full_notified": {}}


def save_state(st: dict) -> None:
    p = state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1))
    tmp.replace(p)


def campaigns_root() -> Path:
    return ec.ax.state_dir() / "campaigns"


def bench_retention(version: str | None = None) -> dict | None:
    """冻结样本上当前规则版本最新一次评分 → 高 / 低价值保留率（分诊与现行闸并排）。没有评分记录返回 None。"""
    version = version or obs_triage.rules_version(obs_triage.load_triage_config())
    runs = sorted(obs_bench.bench_root().glob("obs-*/runs/*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in runs:
        r = json.loads(p.read_text())
        if r.get("rules_version") != version:
            continue
        a = r["splits"]["all"]
        return {"bench_id": r.get("bench_id"), "rules_version": version, "n": a["n"],
                "h_keep_pct": round(100 - a["h_kill_pct"], 1), "h_keep_pct_baseline": round(100 - a["h_kill_pct_baseline"], 1),
                "l_keep_pct": round(100 - a["l_drop_pct"], 1), "l_keep_pct_baseline": round(100 - a["l_drop_pct_baseline"], 1),
                "at": r.get("at")}
    return None


def _pct(v) -> str:
    return "—" if v is None else f"{v:g}%"


def _tok(v) -> str:
    if v is None:
        return "—（没记 token）"
    return f"{v / 10000:.1f} 万" if v >= 10000 else str(v)


def campaign_section(cid: str, summary: dict) -> list[str]:
    t = summary["total"]
    shadow = summary["enabled"] != [True]
    head = f"▍{cid}（{t['items']} 条 / {t['batches']} 批" + ("，已打满" if summary["full"] else "") + "）"
    if not t["triage_batches"]:
        return [head, "  这些批领于分诊上线之前，没有分诊计数"]
    lines = [head,
             f"  过滤：闸拒 {_pct(t['gate_reject_pct'])}｜分诊判丢 {t['triage_drop']}（{_pct(t['triage_drop_pct'])}）"
             f"｜判复核 {t['triage_review']}（{_pct(t['triage_review_pct'])}）"
             + ("" if shadow else f"｜已跳过 {t['triage_dropped']}")]
    if shadow:
        lines.append(f"  token：影子模式，判丢的照样抽了；启用就能省约 {_tok(t['tokens_saveable_est'])}")
    else:
        lines.append(f"  token：已省约 {_tok(t['tokens_saved_est'])}")
    for m, v in sorted(summary["models"].items()):
        if v["triage_batches"]:
            lines.append(f"  · {m}：判丢 {_pct(v['triage_drop_pct'])}｜判复核 {_pct(v['triage_review_pct'])}"
                         f"｜{v['tokens_per_obs'] or '—'} token/条")
    return lines


def build_message(sections: list[tuple[str, dict]], bench: dict | None, *, kind: str) -> str:
    lines = [f"【观测分诊·{kind}】规则 {bench['rules_version'] if bench else obs_triage.rules_version(obs_triage.load_triage_config())}"
             + ("（影子模式）" if not obs_triage.triage_enabled(obs_triage.load_triage_config()) else "（已启用）")]
    if bench:
        lines.append(f"保留率（冻结样本 {bench['n']} 条，分诊 / 现行闸）：高价值 {_pct(bench['h_keep_pct'])} / "
                     f"{_pct(bench['h_keep_pct_baseline'])}｜低价值 {_pct(bench['l_keep_pct'])} / {_pct(bench['l_keep_pct_baseline'])}")
    else:
        lines.append("保留率：当前规则版本还没在冻结样本上评过分（python -m tools.obs_bench score）")
    counted = [(cid, s) for cid, s in sections if s["total"]["triage_batches"]]
    legacy = [cid for cid, s in sections if not s["total"]["triage_batches"]]
    if not counted:
        lines.append("本期没有带分诊计数的领批。")
    for cid, s in counted:
        lines += campaign_section(cid, s)
    if len(counted) > 1:
        tot = {k: sum(s["total"][k] for _, s in counted) for k in ("triaged", "triage_drop", "triage_review")}
        save = [s["total"]["tokens_saveable_est"] for _, s in counted if s["total"]["tokens_saveable_est"] is not None]
        lines.append(f"合计：分诊看过 {tot['triaged']} 条，判丢 {tot['triage_drop']}，判复核 {tot['triage_review']}"
                     + (f"；影子模式下启用可省约 {_tok(sum(save))}" if save else ""))
    if legacy:
        lines.append(f"另有 {len(legacy)} 条战役的批领于分诊上线（10-24 13:56）之前，没有分诊计数：{'、'.join(legacy)}")
    full_shadow = [cid for cid, s in sections if s["full"] and s["enabled"] != [True] and s["total"]["triage_drop"]]
    if full_shadow:
        lines.append(f"下一步：从判丢档抽 20 条复查（python -m tools.triage_notify sample --id {full_shadow[0]}），"
                     "没有高价值就可以把 config/obs_triage.json 的 enabled 改成 true")
    return "\n".join(lines)


def send(msg: str, *, dry: bool, runner=subprocess.run) -> bool:
    print(msg)
    if dry:
        return False
    if not FEISHU_SEND.exists():
        print(f"[feishu] skipped: {FEISHU_SEND} 不存在", file=sys.stderr)
        return False
    try:
        r = runner([sys.executable, str(FEISHU_SEND), msg, "--webhook", "kg-hub", "--title", TITLE],
                   check=False, timeout=20)
        return getattr(r, "returncode", 1) == 0
    except Exception as exc:  # noqa: BLE001
        print(f"[feishu] skipped: {type(exc).__name__}: {exc}", file=sys.stderr)
        return False


def summarize(cid: str, since: str | None = None) -> dict:
    d = ec.campaign_dir(cid)
    return ec.triage_summary(d, ec.load(d), since=since)


def notify_if_full(cid: str, *, dry: bool = False, runner=subprocess.run) -> bool:
    """战役打满且没推过 → 推一条并记下；推送失败不记，下次（含每周任务）再推。"""
    st = load_state()
    if cid in st.get("full_notified", {}):
        return False
    s = summarize(cid)
    if not s["full"] or not s["total"]["triage_batches"]:   # 分诊上线前打满的老战役没有可报的数
        return False
    ok = send(build_message([(cid, s)], bench_retention(), kind="战役打满"), dry=dry, runner=runner)
    if ok:
        st.setdefault("full_notified", {})[cid] = datetime.now(timezone.utc).isoformat()
        save_state(st)
    return ok


def all_campaigns() -> list[str]:
    root = campaigns_root()
    return sorted(p.name for p in root.iterdir() if (p / "campaign.json").exists()) if root.exists() else []


def weekly(*, dry: bool = False, runner=subprocess.run, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=7)).isoformat()
    sections = []
    for cid in all_campaigns():
        s = summarize(cid, since=since)
        if s["total"]["batches"]:
            sections.append((cid, s))
    msg = build_message(sections, bench_retention(), kind="周报")
    send(msg, dry=dry, runner=runner)
    for cid in all_campaigns():          # 漏推的打满通知（例如在 codex 沙箱里提交、当时推不出去）
        notify_if_full(cid, dry=dry, runner=runner)
    return msg


def sample(cid: str, tier: str = "drop", n: int = 20, seed: int = 0) -> list[dict]:
    """从战役批次里取被判为 tier 的条目，随机抽 n 条（id、批次、标题），供人工 / 子代理复查。"""
    d = ec.campaign_dir(cid)
    c = ec.load(d)
    pool = []
    for b in c["batches"]:
        p = d / "batches" / f"{b['id']}.json"
        if b["status"] == "released" or not p.exists():
            continue
        for it in json.loads(p.read_text()).get("items", []):
            if it.get("triage_tier") == tier:
                title = (it.get("episode_body") or "").splitlines()[0] if it.get("episode_body") else ""
                pool.append({"id": it["id"], "batch": b["id"], "title": title[:120]})
    random.Random(seed).shuffle(pool)
    return pool[:n]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("digest")
    s.add_argument("--id", required=True)
    s.add_argument("--dry", action="store_true")
    s = sub.add_parser("weekly")
    s.add_argument("--dry", action="store_true")
    s = sub.add_parser("check-full")
    s.add_argument("--dry", action="store_true")
    s = sub.add_parser("sample")
    s.add_argument("--id", required=True)
    s.add_argument("--tier", default="drop", choices=("drop", "review"))
    s.add_argument("--n", type=int, default=20)
    a = ap.parse_args(argv)
    if a.cmd == "digest":
        send(build_message([(a.id, summarize(a.id))], bench_retention(), kind="战役摘要"), dry=a.dry)
    elif a.cmd == "weekly":
        weekly(dry=a.dry)
    elif a.cmd == "check-full":
        for cid in all_campaigns():
            notify_if_full(cid, dry=a.dry)
    else:
        print(json.dumps(sample(a.id, a.tier, a.n), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
