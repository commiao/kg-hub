"""网关路径的自定义抽取指令：从 config 文件读、可关、内容点名 15 个边名与禁止项。

指令文本本身不依赖 graphiti，所以这里只验证文件契约；kg_hub_server 的接线在容器里由
test_ingest_extracted_handler 同一套环境覆盖导入。"""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
import re
import unittest

ROOT = _Path(__file__).resolve().parent.parent
EDGE_NAMES = ["caused_by", "fixed_by", "verified_by", "references", "modified", "depends_on", "supersedes",
              "derived_from", "occurred_in", "belongs_to", "extracted_from", "relates_to", "documented_in",
              "diagnosed_by", "implemented_as"]
ENTITY_TYPES = ["Person", "Project", "File", "Tool", "Concept", "Issue", "Fix", "Config", "Session",
                "Observation", "Capsule", "KnowledgeDoc", "Lesson"]


class InstructionFileTest(unittest.TestCase):
    def test_file_names_every_schema_name_and_forbids_dirty_entities(self):
        text = (ROOT / "config" / "gateway_extraction_instructions.md").read_text(encoding="utf-8")
        for n in EDGE_NAMES + ENTITY_TYPES:
            self.assertIn(n, text, n)
        self.assertRegex(text, r"commit hash")
        self.assertRegex(text, r"absolute filesystem path")
        self.assertRegex(text, r"T-0123|task id")
        self.assertLessEqual(len(text), 6000)
        # 不能出现仓库里的私有路径或凭证字样
        self.assertNotRegex(text, r"token|password|secret", re.I)

    def test_server_wiring_source(self):
        src = (ROOT / "kg_hub_server.py").read_text(encoding="utf-8")
        self.assertIn('kwargs["custom_extraction_instructions"] = CUSTOM_EXTRACTION_INSTRUCTIONS', src)
        self.assertIn('KG_HUB_CUSTOM_EXTRACTION', src)


if __name__ == "__main__":
    unittest.main()
