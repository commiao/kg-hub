"""线上实际在跑的运行参数，必须能从 git 回答。

2026-09-20 实测：有五个参数只存在于 NAS 的 `.env` 里，而 `.env` 不在任何 git 仓库。
任何人 clone 本仓库读 `docker-compose.yml`，看到的是另一套数字：

    温控阈值       线上 59    git 52     ← 用户 09-17 明确拍板上调的
    抽取并发       线上 2     git 1
    调用最小间隔   线上 2.0   git 4.0    ← 相当于把模型调用速率翻了一倍
    预消化         线上 1     git 0
    承载盘         线上 sata3 git （空）

代价当天就付了：排查「积压为什么不动」时，先按 git 的 52 推了好几轮，
才发现真值是 59。**判据的参照物必须是别人也能独立算出同一个答案的那个东西**，
一台机器上的 `.env` 不是。

这份测试把这五个值钉在这里。它不是防止有人改这些参数 —— 参数本来就该能调；
它防的是**改了而 git 看不出来**。要改，就得连同这里的理由一起改，那一刻它就
变成了一个被记录的决定，而不是某台机器上的一个事实。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = (ROOT / "docker-compose.yml").read_text("utf-8")

# 值 → 这个值是怎么定下来的。没有出处的数字迟早被人当成手滑改掉。
DECIDED = {
    "KG_HUB_REFINERY_MAX_DISK_TEMP": ("59", "用户 2026-09-17 拍板上调；DSM 约 61°C 强制关机"),
    "KG_HUB_SEMAPHORE_LIMIT":        ("2",  "抽取并发；与 INGEST_CONCURRENCY 保持 2 同源"),
    "KG_HUB_LLM_MIN_INTERVAL_SEC":   ("2.0", "两次模型调用的最小间隔"),
    "KG_HUB_PREDIGEST":              ("1",  "预消化开关，线上开启"),
    "KG_HUB_REFINERY_DATA_DISK":     ("sata3", "承载数据卷那块盘；仅用于遥测单列一栏，判定仍取 max(全部盘)"),
}


class ComposeOperatingParamsTests(unittest.TestCase):
    def test_defaults_are_the_values_actually_running(self):
        for key, (value, why) in DECIDED.items():
            m = re.search(r"\$\{" + key + r":-([^}]*)\}", COMPOSE)
            self.assertIsNotNone(m, f"{key} 不在 compose 里，线上值就没有 git 正本")
            self.assertEqual(
                m.group(1), value,
                f"{key} 的 git 默认值与线上不符。{why}。"
                "要改就连同这里的理由一起改 —— 否则下一个人读 git 会得到假答案")

    def test_every_decided_value_carries_its_reason_in_the_compose_file(self):
        """光有数字不够：紧挨着它的注释要说清楚这个值是怎么来的。

        没有出处的数字会被当成手滑改掉 —— 准则里那条「没有事故支撑的规则会被
        当教条绕过」，对数字同样成立。
        """
        # 必须是**紧挨着的上一行**。原先写成"前 4 行里有 # 就算数",而 compose 里
        # 到处是注释 —— 2026-09-20 变异验证当场抓到：删掉真正那条说明，测试照样全绿。
        lines = COMPOSE.splitlines()
        for key in DECIDED:
            idx = next(i for i, l in enumerate(lines) if "${" + key + ":-" in l)
            self.assertTrue(
                idx > 0 and lines[idx - 1].strip().startswith("#"),
                f"{key} 的上一行不是注释 —— 没有出处的数字会被当成手滑改掉")


if __name__ == "__main__":
    unittest.main()
