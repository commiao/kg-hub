"""NAS `.env` 里的非机密配置不许和 git 说的不一样 —— 而且这个检查一眼都不看机密。

准则 19 的判据不是「.env 进 git」，是逐键问「改了线上行为会变吗 / 含机密吗」。
2026-09-20 的 87a2dad 已经把五个非机密运行参数的默认值搬进 docker-compose.yml，
但 `.env` 里那几行没删 —— 值现在恰好一样，所以看不出问题；谁在 NAS 上改一下，
git 就又开始说假话。准则 2：只搬值不建检测，下次漂移照样没人知道。

最要紧的一条是**机密不出 NAS**：这个工具先只取键名，再按白名单在远端过滤，
只有非机密键的值会过网络。那条过滤单独成了函数，就是为了能直接验它。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy" / "nas"))

import check_env_drift as ced  # noqa: E402

ALL_KEYS = ["FALKORDB_PASSWORD", "KG_HUB_API_TOKEN", "KG_HUB_MODEL_GATEWAY_TOKEN",
            "KG_HUB_FEISHU_WEBHOOK", "KG_HUB_DATA_ROOT", "KG_HUB_IMAGE_TAG",
            "KG_HUB_IMAGE_TAG_PREV", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL",
            "KG_HUB_REFINERY_BACKLOG", "KG_HUB_REFINERY_WINDOW_START",
            "KG_HUB_REFINERY_WINDOW_END"]
DEFAULTS = {"ANTHROPIC_BASE_URL": "http://model-gateway:39000",
            "ANTHROPIC_MODEL": "kg_hub.entity_extract",
            "KG_HUB_REFINERY_BACKLOG": "1",
            "KG_HUB_REFINERY_WINDOW_START": "22",
            "KG_HUB_REFINERY_WINDOW_END": "8"}


class SecretsNeverLeaveTests(unittest.TestCase):
    def test_no_secret_key_is_ever_read(self):
        readable = set(ced.readable_keys(ALL_KEYS))
        leaked = readable & ced.SECRET_KEYS
        self.assertEqual(leaked, set(), f"这些机密键的值会被取回来：{leaked}")

    def test_machine_specific_keys_are_not_read_either(self):
        """它们本来就该只活在 .env 里，取回来也只会制造误报。"""
        self.assertEqual(set(ced.readable_keys(ALL_KEYS)) & ced.MACHINE_KEYS, set())

    def test_the_webhook_counts_as_a_secret(self):
        """URL 自带令牌 —— 按普通配置处理就等于把令牌打印出来。"""
        self.assertIn("KG_HUB_FEISHU_WEBHOOK", ced.SECRET_KEYS)

    def test_the_image_tag_is_never_compared_against_git(self):
        """它是「线上是哪个 commit」，写进 git 会自相矛盾。"""
        self.assertIn("KG_HUB_IMAGE_TAG", ced.MACHINE_KEYS)


class VerdictTests(unittest.TestCase):
    def classify(self, values, keys=None, defaults=None):
        return ced.classify(keys or ALL_KEYS, values, defaults or DEFAULTS)

    def test_a_differing_value_is_fatal(self):
        """这就是 87a2dad 要治的病：git 说 8，线上跑 10。"""
        fatal, _ = self.classify({**{k: DEFAULTS[k] for k in DEFAULTS},
                                  "KG_HUB_REFINERY_WINDOW_END": "10"})
        self.assertTrue(any("WINDOW_END" in f and "说假话" in f for f in fatal), fatal)

    def test_a_key_git_never_heard_of_is_fatal(self):
        fatal, _ = self.classify({**DEFAULTS, "KG_HUB_SOMETHING_NEW": "7"},
                                 keys=ALL_KEYS + ["KG_HUB_SOMETHING_NEW"])
        self.assertTrue(any("SOMETHING_NEW" in f for f in fatal), fatal)

    def test_a_missing_secret_is_fatal(self):
        fatal, _ = self.classify(DEFAULTS,
                                 keys=[k for k in ALL_KEYS if k != "KG_HUB_API_TOKEN"])
        self.assertTrue(any("KG_HUB_API_TOKEN" in f for f in fatal), fatal)

    def test_an_identical_value_is_only_a_warning(self):
        """线上行为没问题，但它是下一次漂移的入口 —— 提示，不拦。"""
        fatal, warn = self.classify(dict(DEFAULTS))
        self.assertEqual(fatal, [])
        self.assertEqual(len(warn), len(DEFAULTS))

    def test_a_clean_env_says_so(self):
        """.env 里只剩机密与机器特定键时，既不该报错也不该报警。"""
        keys = [k for k in ALL_KEYS if k in ced.SECRET_KEYS or k in ced.MACHINE_KEYS]
        fatal, warn = ced.classify(keys, {}, DEFAULTS)
        self.assertEqual((fatal, warn), ([], []))


class ParseTests(unittest.TestCase):
    def test_defaults_come_from_the_real_compose(self):
        """期望值和被测对象取自同一来源（准则 28），不在测试里抄第二份。"""
        found = ced.git_defaults(ROOT)
        for key, value in DEFAULTS.items():
            self.assertEqual(found.get(key), value, key)

    def test_it_reuses_release_sh_for_the_ssh_target(self):
        """不写第二份 ssh 默认值 —— 那边一改，这里就会对着错的机器报「一切正常」。"""
        src = (ROOT / "deploy" / "nas" / "check_env_drift.py").read_text("utf-8")
        self.assertIn("from check_source_drift import release_config", src)
        self.assertNotIn("commiao@", src)


if __name__ == "__main__":
    unittest.main()
