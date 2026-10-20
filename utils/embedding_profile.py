"""向量模型档案：模型名、维度、实体消解门槛的单一事实源（T-0339）。

为什么要有它：向量模型一换，三样东西必须一起换——查询时现算向量用的模型、图里存的向量、
按该模型分数分布校准过的门槛。任何一样没跟上都不会报错，只会让检索和实体消解静默变差。
所以：
- 运行时用哪个模型只由环境变量 KG_HUB_EMBED_MODEL 决定，缺省是现状 bge-small-en；
  写了未登记的模型名直接报错，不猜。
- 图里的向量出自哪个模型，记在图中唯一的 (:EmbeddingProfile {key:'active'}) 节点上，
  由 tools/reembed.py 在全量写回完成后才更新；没有这个节点 = 从未重算过 = 缺省模型。
- build_graphiti() 建客户端时比对两者，不一致就拒绝启动（assert_graph_profile）。
  宁可服务起不来，也不要拿新模型的查询向量去比旧模型的库向量。

方案与评测：docs/EMBEDDING-MODEL-SWITCH-PLAN.md。
"""
from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
ENV_VAR = "KG_HUB_EMBED_MODEL"
MARKER_LABEL = "EmbeddingProfile"


@dataclass(frozen=True)
class Profile:
    model: str
    dim: int
    # 实体复用的向量候选门槛：分数 = (1 + cos) / 2，取前 5 个候选再过 _lexically_same。
    # 每个模型的分数分布不同，必须各自校准（见方案 §3 与 tests/test_embedding_profile.py）。
    node_match_min: float


PROFILES: dict[str, Profile] = {
    "BAAI/bge-small-en-v1.5": Profile("BAAI/bge-small-en-v1.5", 384, 0.95),
    # 2026-10-20 校准：全图 5,651 对「_lexically_same 为真」的实体对，按线上规则（分数过门槛且在前 5 候选内）
    # 统计复用成功率——bge-small-en@0.95 为 42.8%；MiniLM@0.95 只有 16.7%（会多出大量重复实体），
    # @0.89 为 42.9%，与现状持平；平均每实体多约 1 个候选交给词面门过滤，不会误并。
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2": Profile(
        "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2", 384, 0.89),
}


class EmbeddingProfileError(RuntimeError):
    """配置的模型未登记，或与图里向量的来源模型不一致。"""


def active_model(env: dict | None = None) -> str:
    env = os.environ if env is None else env
    name = (env.get(ENV_VAR) or "").strip() or DEFAULT_MODEL
    if name not in PROFILES:
        raise EmbeddingProfileError(
            f"{ENV_VAR}={name!r} 未登记；可选：{', '.join(sorted(PROFILES))}")
    return name


def active_profile(env: dict | None = None) -> Profile:
    return PROFILES[active_model(env)]


def check_dim(profile: Profile, env: dict | None = None) -> None:
    """EMBEDDING_DIM 是 graphiti 的冻结字段，必须与模型维度一致，否则向量写进去也查不出来。"""
    env = os.environ if env is None else env
    raw = (env.get("EMBEDDING_DIM") or "").strip()
    if raw and int(raw) != profile.dim:
        raise EmbeddingProfileError(
            f"EMBEDDING_DIM={raw} 与 {profile.model} 的维度 {profile.dim} 不一致")


MARKER_READ = (f"MATCH (p:{MARKER_LABEL} {{key: 'active'}}) "
               "RETURN p.model AS model, p.updated_at AS updated_at LIMIT 1")
MARKER_WRITE = (f"MERGE (p:{MARKER_LABEL} {{key: 'active'}}) "
                "SET p.model = $model, p.updated_at = $updated_at, p.updated_by = $updated_by "
                "RETURN p.model AS model")


def graph_model_from_rows(rows) -> str:
    """标记节点的查询结果 → 图里向量的来源模型；没有节点 = 从未重算 = 缺省模型。"""
    for r in rows or []:
        m = r.get("model") if isinstance(r, dict) else (r[0] if r else None)
        if m:
            return str(m)
    return DEFAULT_MODEL


def check_graph_profile(graph_model: str, configured: str) -> None:
    if graph_model != configured:
        raise EmbeddingProfileError(
            f"图里的向量出自 {graph_model}，但本进程配置的是 {configured}（{ENV_VAR}）。"
            "查询向量与库向量必须同源：要么把环境变量改回去，要么先用 tools/reembed.py 全量重算写回。")


async def read_graph_model(driver) -> str:
    rows, _, _ = await driver.execute_query(MARKER_READ)
    return graph_model_from_rows(rows)


async def assert_graph_profile(driver, env: dict | None = None) -> Profile:
    """建客户端时调用：配置未登记、维度不符、或与图里向量来源不一致 → 抛错，拒绝继续。"""
    profile = active_profile(env)
    check_dim(profile, env)
    check_graph_profile(await read_graph_model(driver), profile.model)
    return profile
