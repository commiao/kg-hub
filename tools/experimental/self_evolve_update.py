"""tools/self_evolve_update.py — the self-evolving update pass (SELF-EVOLVING §2).

Ties the pieces together:
  injection log → matched session → scenario (classify) → RewardProvider[scenario]
  → reward per injected capsule → ScoreStore.update((capsule, scenario), reward)

Idempotent: each (session, capsule, scenario) folds in at most once, so it is safe to
re-run / cron over overlapping windows. Only `coding` has a provider now; other
scenarios route to None → abstain (no score change). reward=None also abstains.

Run manually for now; later a launchd cron can call it after sessions accumulate.

Usage:
  python -m tools.self_evolve_update                 # update + print report
  python -m tools.self_evolve_update --dry           # compute, do NOT persist
  python -m tools.self_evolve_update --since 2026-06-16
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter

from tools.experimental.engagement_audit import load_injections, parse_ts
from tools.experimental.scenario_classifier import classify, load_session_features, sessions_by_project, match
from tools.experimental.capsule_score import ScoreStore
from tools.experimental.coding_reward import CodingReward

# Scenario → RewardProvider. None = reserved/unimplemented → abstain.
REWARD_PROVIDERS = {
    "coding": CodingReward(),
    "ops": None,
    "writing": None,
    "research": None,
    "planning": None,
    "unknown": None,
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-06-16")
    ap.add_argument("--dry", action="store_true", help="compute but do not persist the store")
    args = ap.parse_args()

    feats = load_session_features()
    by_proj = sessions_by_project(feats)
    injections = load_injections(parse_ts(args.since + "T00:00:00Z"))
    store = ScoreStore()

    # Prepare providers that need it (CodingReward fetches capsule texts from server).
    prepared: dict[str, object] = {}
    for scen, prov in REWARD_PROVIDERS.items():
        if prov is None:
            continue
        try:
            prov.prepare()
            prepared[scen] = prov
        except Exception as exc:
            print(f"[self_evolve] provider {scen} prepare failed: "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr)
    if not prepared:
        print("[self_evolve] no usable provider (kg-hub server unreachable?)", file=sys.stderr)
        return 1

    tally = Counter()   # outcomes: updated_1 / updated_0 / abstain / skip_seen / no_provider / unmatched
    for inj in injections:
        sess = match(inj, by_proj)
        if not sess:
            tally["unmatched"] += 1
            continue
        scen, _ = classify(sess)
        prov = prepared.get(scen)
        for cap in inj["names"]:
            if prov is None:
                tally["no_provider"] += 1
                continue
            token = f"{sess['sid']}|{cap}|{scen}"
            if store.seen(token):
                tally["skip_seen"] += 1
                continue
            r = prov.reward(sess, cap)
            if r is None:
                tally["abstain"] += 1
                continue
            store.update(cap, scen, r)
            store.mark(token)
            tally["updated_1" if r >= 0.5 else "updated_0"] += 1

    if not args.dry:
        store.save()

    print(f"# 自进化更新通路 — since {args.since}{' (DRY)' if args.dry else ''}")
    print()
    print("处理统计：")
    for k in ["updated_1", "updated_0", "abstain", "skip_seen", "no_provider", "unmatched"]:
        print(f"  {k:12} = {tally.get(k, 0)}")
    print()
    print("当前 (胶囊, 场景) 得分：")
    print(store.report())
    print()
    print("说明：updated_1=判定有贡献 / updated_0=注入了但没用上或会话失败 / "
          "abstain=outcome 测不到弃权 / no_provider=该场景未实现。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
