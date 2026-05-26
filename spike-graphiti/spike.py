"""
kg-hub Graphiti SPIKE
Goal: validate whether Graphiti can ingest OpenClaw capsule narratives and
materialize the 5-hop causal chain example OpenClaw gave us.

LLM    : qwen3.6-plus via 百炼 Anthropic-compatible endpoint (reused from claude-mem)
Embed  : fastembed (local, BAAI/bge-small-en-v1.5, 384-dim)
Rerank : noop pass-through
Graph  : Kuzu embedded -> ./kuzu_db/
"""

import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# IMPORTANT: must be set before importing graphiti_core because EMBEDDING_DIM is frozen
os.environ["EMBEDDING_DIM"] = "384"

from dotenv import load_dotenv

load_dotenv(Path.home() / ".claude-mem" / ".env", override=True)

from anthropic import AsyncAnthropic
from graphiti_core import Graphiti
from graphiti_core.cross_encoder.client import CrossEncoderClient
from graphiti_core.driver.kuzu_driver import KuzuDriver
from graphiti_core.embedder.client import EmbedderClient
from graphiti_core.llm_client import LLMConfig
from graphiti_core.llm_client.anthropic_client import AnthropicClient
from graphiti_core.nodes import EpisodeType


# ----- LLM client: wrap qwen3.6-plus via 百炼 Anthropic adapter -----
def build_llm() -> AnthropicClient:
    auth_token = os.environ["ANTHROPIC_AUTH_TOKEN"]
    base_url = os.environ["ANTHROPIC_BASE_URL"]
    model = os.environ.get("ANTHROPIC_MODEL", "qwen3.6-plus")
    cfg = LLMConfig(api_key=auth_token, model=model, max_tokens=4096)
    async_client = AsyncAnthropic(auth_token=auth_token, base_url=base_url, max_retries=1)

    # 百炼 qwen3.6-plus runs in thinking mode by default, which forbids
    # forced tool_choice. Inject thinking={"type":"disabled"} on every call.
    orig_create = async_client.messages.create

    async def create_with_thinking_off(*args, **kwargs):
        extra_body = dict(kwargs.get("extra_body") or {})
        extra_body.setdefault("thinking", {"type": "disabled"})
        kwargs["extra_body"] = extra_body
        return await orig_create(*args, **kwargs)

    async_client.messages.create = create_with_thinking_off
    return AnthropicClient(config=cfg, client=async_client)


# ----- Embedder: fastembed local -----
class FastembedEmbedder(EmbedderClient):
    def __init__(self):
        from fastembed import TextEmbedding
        self.model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")

    async def create(self, input_data):
        if isinstance(input_data, str):
            texts = [input_data]
        else:
            texts = list(input_data)
        emb = list(self.model.embed(texts))
        return emb[0].tolist() if len(emb) == 1 else emb[0].tolist()

    async def create_batch(self, input_data_list):
        emb = list(self.model.embed(input_data_list))
        return [e.tolist() for e in emb]


# ----- Cross encoder: noop pass-through (we test ingest, not ranking) -----
class NoOpCrossEncoder(CrossEncoderClient):
    async def rank(self, query, passages):
        return [(p, 1.0) for p in passages]


# ----- The 5 OpenClaw episodes (real samples) -----
EPISODES = [
    {
        "name": "capsule-CAPSULE-HOOK-SYSTEM-ARCH-2026-03-20",
        "body": (
            "知识胶囊 CAPSULE-HOOK-SYSTEM-ARCH-2026-03-20 标题《钩子任务执行系统架构》"
            "创建于 2026-03-20，类型=架构设计，质量评分 5.0/5，使用次数 3 次。"
            "标签：architecture, hook-system, task-management, three-tier, p0。"
            "适用场景：需要自动化任务执行时。来源：Coordinator 任务管理系统专项会话。"
            "胶囊关联 OpenClaw 用户 jingmiao@liblib.ai。"
        ),
        "source": EpisodeType.text,
        "desc": "OpenClaw capsule metadata",
    },
    {
        "name": "memory-bank-billing-principle",
        "body": (
            "MEMORY.md 概念：银行账单记账原则。内容：银行出具的账单和交易明细视为 100% 正确，"
            "任何出入严格以银行数据为准。关联到 光大银行数据源、记账规则、accounts.json 三个对象。"
            "确立时间 2026-05-14。所属用户：jingmiao@liblib.ai。"
        ),
        "source": EpisodeType.text,
        "desc": "OpenClaw MEMORY.md concept",
    },
    {
        "name": "knowledge-doc-feishu-image-upload",
        "body": (
            "知识库文档 feishu-image-upload-complete-guide.md 路径 notes/knowledge-base/。"
            "类型：操作指南。关联到 feishu-image-sender 技能。"
            "记录了如何通过飞书 API 上传图片消息。"
        ),
        "source": EpisodeType.text,
        "desc": "OpenClaw KnowledgeDoc",
    },
    {
        "name": "issue-cron-notification-failure",
        "body": (
            "问题：Cron 通知发送失败。根因：飞书 chat_id 硬编码分散在 60 处脚本中，"
            "配置分散无法统一管理。这个根因导致了一个进一步问题——投资晚报被发送到了战略规划群"
            "（本应发往财务管家群），落户监控被发送到系统监控群（本应发往考公群）。"
            "该问题在 CAPSULE-NOTIFICATION-ROUTE-2026 胶囊中被诊断分析。"
        ),
        "source": EpisodeType.text,
        "desc": "OpenClaw real issue narrative",
    },
    {
        "name": "fix-notification-route-2026-03-20",
        "body": (
            "胶囊 CAPSULE-NOTIFICATION-ROUTE-2026 标题《通知路由统一配置系统》"
            "提出的解决方案：SQLite 统一配置 + CLI 工具。"
            "具体落地为两个工件：notification-route.db（统一数据库）和 notify-send.sh（CLI 工具），"
            "所有原本硬编码 chat_id 的脚本统一改用 notify-send.sh 接口。"
            "该修复在 2026-03-20 通过实战演练验证，质量评分 5.0/5。"
            "修复解决了上面那个 Cron 通知发送失败的根本问题。"
        ),
        "source": EpisodeType.text,
        "desc": "OpenClaw fix narrative",
    },
]


async def main():
    db_path = Path(__file__).parent / "kuzu_db"
    skip_ingest = "--reuse" in sys.argv
    if not skip_ingest:
        if db_path.exists():
            import shutil
            if db_path.is_dir():
                shutil.rmtree(db_path)
            else:
                db_path.unlink()
        for sib in db_path.parent.glob("kuzu_db*"):
            if sib.is_file():
                sib.unlink()
            elif sib.is_dir():
                import shutil
                shutil.rmtree(sib)

    print(f"[init] kuzu db: {db_path}")
    driver = KuzuDriver(db=str(db_path))

    print("[init] llm: qwen3.6-plus via 百炼")
    llm = build_llm()

    print("[init] embedder: fastembed BAAI/bge-small-en-v1.5 (first call downloads ~33MB)")
    embedder = FastembedEmbedder()

    print("[init] graphiti")
    g = Graphiti(
        graph_driver=driver,
        llm_client=llm,
        embedder=embedder,
        cross_encoder=NoOpCrossEncoder(),
    )
    await g.build_indices_and_constraints()

    # graphiti-core 0.29 Kuzu driver does not auto-create FTS indices.
    # Create them manually (matches queries hardcoded into search_utils).
    import kuzu as _kz
    _conn = _kz.Connection(driver.db)
    _conn.execute("INSTALL fts;")
    _conn.execute("LOAD fts;")
    for _stmt in [
        "CALL CREATE_FTS_INDEX('Episodic', 'episode_content', ['content', 'source', 'source_description'])",
        "CALL CREATE_FTS_INDEX('Entity', 'node_name_and_summary', ['name', 'summary'])",
        "CALL CREATE_FTS_INDEX('Community', 'community_name', ['name'])",
        "CALL CREATE_FTS_INDEX('RelatesToNode_', 'edge_name_and_fact', ['name', 'fact'])",
    ]:
        try:
            _conn.execute(_stmt)
        except Exception as e:
            print(f"  (FTS index already exists or unavailable: {e})")
    _conn.close()
    print("[init] FTS indices ready")

    if skip_ingest:
        print("[ingest] --reuse: skipping LLM ingest, inspecting existing DB")
    else:
        print(f"\n[ingest] adding {len(EPISODES)} episodes...")
        now = datetime.now(tz=timezone.utc)
        for i, ep in enumerate(EPISODES):
            print(f"  [{i+1}/{len(EPISODES)}] {ep['name']}")
            try:
                result = await g.add_episode(
                    name=ep["name"],
                    episode_body=ep["body"],
                    source=ep["source"],
                    source_description=ep["desc"],
                    reference_time=now,
                )
                print(f"      entities={len(result.nodes)} edges={len(result.edges)}")
            except Exception as e:
                print(f"      FAILED: {type(e).__name__}: {e}")
                import traceback
                traceback.print_exc()
                sys.exit(2)

    print("\n[query] dumping all nodes & edges from kuzu...")
    import kuzu
    conn = kuzu.Connection(driver.db)

    print("\n--- Entity nodes ---")
    res = conn.execute("MATCH (n:Entity) RETURN n.name, n.labels, n.summary LIMIT 50")
    while res.has_next():
        row = res.get_next()
        print(f"  - {row[0]}  labels={row[1]}")
        if row[2]:
            print(f"      summary: {row[2][:120]}")

    print("\n--- Edges (via RelatesToNode_ intermediate) ---")
    res = conn.execute(
        "MATCH (a:Entity)-[:RELATES_TO]->(e:RelatesToNode_)-[:RELATES_TO]->(b:Entity) "
        "RETURN a.name, e.name, e.fact, b.name LIMIT 200"
    )
    edge_count = 0
    while res.has_next():
        row = res.get_next()
        edge_count += 1
        print(f"  ({row[0]}) -[{row[1]}]-> ({row[3]})")
        if row[2]:
            print(f"      fact: {row[2][:160]}")
    print(f"\n  total edges: {edge_count}")

    print("\n[search] 'Cron 通知失败怎么修的'")
    edges = await g.search(query="Cron 通知失败怎么修的", num_results=5)
    for e in edges:
        print(f"  - {e.fact[:140]}")

    print("\n[done] kuzu db kept at:", db_path)


if __name__ == "__main__":
    asyncio.run(main())
