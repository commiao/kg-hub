"""预抽取入库：调用方自带实体/关系，服务端只做嵌入、消解、写图，不调模型。

为什么要有这条路（2026-10-17 用户决定）：对比「旧模型经网关抽取的存量」与「Claude 会话自行
抽取的新内容」，期间 ``kg_hub.entity_extract`` 断路器保持断开，所以抽取不能再经
``graphiti.add_episode``（那条路的每一步都要打模型）。这条路由 Claude Code 会话把观测抽成
schema v0.2 的实体/关系 JSON，POST 过来；服务端按 graphiti 0.29 同样的图结构落图，
并在 episode / 新实体 / 新边上打 ``extractor_model`` 来源标记，使新旧内容可按模型分组对比。

与 /api/ingest 共享的治理件（备份、IngestedKey 幂等、task_executions 记账、写锁、
origin/kind 打标）都在 kg_hub_server 里复用；本模块只负责**纯逻辑**（校验/归一）和**写图**。

图结构与 graphiti_core 0.29.0 的 save 语句逐字段一致（节点 ``SET n = $entity_data`` +
``vecf32`` 向量；边 ``MERGE (a)-[e:RELATES_TO {uuid}]->(b)``；``MENTIONS`` 边），所以
kg_search / episode_search / normalize_edge_names / quality 画像都把它当普通数据。

消解策略（不调模型，只用本地 bge-small 向量）：
  实体  先按同 group 内名字大小写不敏感精确匹配；再按 name_embedding 余弦 ≥ NODE_MATCH_MIN
        取最相似一个；都没有才建新节点。复用的旧节点**不改**其 summary/labels。
  边    同 (源, 目标, 边名) 且 fact 原文相同的 RELATES_TO 已有 → 只把本 episode 追加进 e.episodes；
        否则建新边（同一对实体之间允许多条不同 fact 的边，与 graphiti 自身一致）。
纪律度量在归一阶段就记下来（schema 外边名、未知实体类型、悬空边、重复三元组），
随响应返回，供每 500 条复盘用；schema 外边名映射为 relates_to 并把原名保留在 raw_name。
"""
from __future__ import annotations

import re
import uuid as uuidlib
import weakref
from dataclasses import dataclass, field
from datetime import datetime, timezone

from pydantic import BaseModel, Field

# 2026-10-17 冒烟实测：bge-small-en 对中文短名的余弦普遍偏高，0.92 把「实际额度来源修正发布」
# 并到了「反思改进项落实专项」、把「…本机测试 FileNotFoundError」并到了「docs/effective-quota」。
# 所以向量只负责召回候选，复用还要过词面校验（_lexically_same）。
# 门槛随向量模型走：每个模型的分数分布不同，值登记在 utils/embedding_profile.PROFILES（T-0339）。
# bge-small-en 仍是 0.95；换模型时在那里按校准结果改，不在这里改。
from utils.embedding_profile import active_profile as _active_embed_profile  # noqa: E402
NODE_MATCH_MIN = _active_embed_profile().node_match_min
# 第 2 轮（opus）复盘：同类型近名仍误并 17 对——只差一个字符/数字/日期（test_phase1m→1p、准则 19–22→26–28）、
# 跨仓库同名文件（kg-hub 与 credvault 的 release.sh）、描述性名字被泛名吸进（max_tokens 路由上限裁剪→max_tokens）。
# 所以：代码类名字（标识符/路径）只认完全相等；只在数字上不同的一律不是同一对象；包含规则占比提到 0.8；
# difflib 阈值提到 0.85。
LEXICAL_MIN_RATIO = 0.85
CONTAIN_MIN_SHARE = 0.8
# 标识符/路径一类的名字：ASCII 字母数字加 . / _ - : ( ) 组成，没有空格和非 ASCII
_CODE_LIKE = re.compile(r"^[A-Za-z0-9_./:\-()~@]+$")
# 后缀合并只给「像文件」的名字：最后一段带扩展名（或是 Dockerfile/Makefile 这类无扩展名的文件）。
# 2026-10-19 单次模式误并抽查：`edge_similarity_search/node_similarity_search` 被当成路径并进了
# `node_similarity_search`——它是两个标识符的复合名，不是目录加文件。
_FILE_TAIL = re.compile(r"(?:^|/)(?:[^/]+\.[A-Za-z0-9]{1,8}|Dockerfile|Makefile|Procfile)$")
# 太泛的文件名：很多仓库都有，只凭「是某个全路径的后缀」不能认定是同一个文件
# （同次抽查：`push/config.go` → `config.go`、`deploy/docker-compose.yml` → `docker-compose.yml`）。
GENERIC_BASENAMES = frozenset({
    "config.go", "main.go", "server.go", "client.go", "router.go", "handler.go", "service.go", "task.go",
    "www.go", "util.go", "utils.go", "types.go", "go.mod", "main.py", "app.py", "server.py", "client.py",
    "utils.py", "util.py", "config.py", "settings.py", "models.py", "views.py", "__init__.py", "setup.py",
    "conftest.py", "index.ts", "index.js", "index.tsx", "types.ts", "index.html", "readme.md",
    "changelog.md", "dockerfile", "makefile", "package.json", "tsconfig.json", "docker-compose.yml",
    "docker-compose.yaml", "compose.yml", "compose.yaml", "config.yml", "config.yaml", "config.json",
    "requirements.txt", "pom.xml", "build.gradle", ".env", ".gitignore",
})
MAX_ENTITIES = 60
MAX_EDGES = 120
MAX_NAME_LEN = 200
MAX_SUMMARY_LEN = 2000
MAX_FACT_LEN = 1000
EXTRACTOR_MODEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}\Z")
# 作为独立实体没有意义的名字：commit 哈希、绝对路径、任务号（见报告第 3 节「脏命名」）。
DIRTY_NAME_PATTERNS = (
    re.compile(r"^(commit\s+)?[0-9a-f]{7,40}$", re.I),
    re.compile(r"^/(Users|volume|home|tmp|private)/"),
    re.compile(r"^T-\d{3,5}$"),
)


class ExtractedEntity(BaseModel):
    name: str = Field(min_length=1, max_length=MAX_NAME_LEN)
    type: str = Field(min_length=1, max_length=40)
    summary: str = Field("", max_length=MAX_SUMMARY_LEN)


class ExtractedEdge(BaseModel):
    source: str = Field(min_length=1, max_length=MAX_NAME_LEN)
    target: str = Field(min_length=1, max_length=MAX_NAME_LEN)
    name: str = Field(min_length=1, max_length=80)
    fact: str = Field(min_length=1, max_length=MAX_FACT_LEN)


@dataclass
class NormalizedEntity:
    key: str            # 小写归一后的名字，payload 内去重用
    name: str
    type: str
    summary: str
    raw_type: str


@dataclass
class NormalizedEdge:
    source_key: str
    target_key: str
    name: str
    fact: str
    raw_name: str


@dataclass
class NormalizedExtraction:
    entities: list[NormalizedEntity]
    edges: list[NormalizedEdge]
    stats: dict = field(default_factory=dict)


def _norm_key(name: str) -> str:
    return " ".join(name.split()).lower()


_LEX_STRIP = re.compile(r"[\s\-_./\\:：（）()\[\]【】,，;；'\"`·]+")


def _lex(name: str) -> str:
    return _LEX_STRIP.sub("", (name or "").lower())


_REPO_PREFIX = re.compile(r"^([a-z0-9][a-z0-9._\-]*):(?!//)(?=[^/:\s])")


def _repo_prefix(name: str) -> str | None:
    """`仓库名:路径`（规则 16）的仓库名；`https://`、Windows 盘符这类不算。"""
    m = _REPO_PREFIX.match(name)
    if not m or len(m.group(1)) < 2:
        return None
    return m.group(1)


# 只差一组对立词的名字是不同的东西（2026-10-22 三模型对比第 6 轮：`prepare 阶段边组相似度` 被并进
# `prepare 阶段节点组相似度`，difflib 0.9 放行）。与「只差数字」同级：在包含/相似度判断之前拦下。
_CONTRAST_PAIRS = (
    ("边", "节点"), ("edge", "node"), ("读", "写"), ("read", "write"), ("新", "旧"), ("new", "old"),
    ("启用", "禁用"), ("enable", "disable"), ("输入", "输出"), ("input", "output"), ("请求", "响应"),
    ("request", "response"), ("源", "目标"), ("source", "target"), ("上限", "下限"), ("min", "max"),
    ("前", "后"), ("before", "after"), ("成功", "失败"), ("success", "failure"), ("开", "关"),
    ("open", "close"), ("入", "出"), ("in", "out"), ("加", "减"), ("增", "删"), ("主", "备"),
    ("primary", "replica"), ("本地", "远端"), ("local", "remote"),
)


def _differ_by_contrast(x: str, y: str) -> bool:
    """x、y 已去分隔符小写化；把 x 里的某个对立词换成另一半能得到 y → 只差一组对立词。"""
    for p, q in _CONTRAST_PAIRS:
        for u, v in ((p, q), (q, p)):
            start = x.find(u)
            while start != -1:
                if x[:start] + v + x[start + len(u):] == y:
                    return True
                start = x.find(u, start + 1)
    return False


# 只差一个词头前缀的名字也是另一个东西（2026-10-22 补交 b0024：`prevalidate 阶段相似度计数` 被并进
# `validate 阶段相似度计数`，包含占比 0.81 放行）。英文：同位置的词多了粘连的字母前缀，或多出一个独立的
# 前缀词（pre-validate）；中文：多出一个否定/前置字（预校验 ↔ 校验、未提交 ↔ 提交）。
_EN_PREFIX_WORDS = frozenset({"pre", "post", "re", "un", "non", "de", "dis", "sub", "anti", "semi", "multi"})
_CJK_PREFIX_CHARS = frozenset("预非未不反无子伪逆")
_ASCII_LETTERS = re.compile(r"^[a-z]+$")


def _differ_by_prefix(a: str, b: str) -> bool:
    ta, tb = _LEX_STRIP.sub(" ", a.lower()).split(), _LEX_STRIP.sub(" ", b.lower()).split()
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if len(short) == len(long_):
        diff = [i for i in range(len(short)) if short[i] != long_[i]]
        if len(diff) == 1:
            s, t = short[diff[0]], long_[diff[0]]
            s, t = (s, t) if len(s) <= len(t) else (t, s)
            head = t[:len(t) - len(s)]
            if t.endswith(s) and s[:1].isascii() and s[:1].isalpha() and len(head) >= 2 and _ASCII_LETTERS.match(head):
                return True
    elif len(long_) == len(short) + 1:
        for i, w in enumerate(long_):
            if w in _EN_PREFIX_WORDS and long_[:i] + long_[i + 1:] == short and i + 1 < len(long_):
                return True
    x, y = "".join(ta), "".join(tb)
    short_s, long_s = (x, y) if len(x) <= len(y) else (y, x)
    if len(long_s) == len(short_s) + 1:
        for i, ch in enumerate(long_s[:-1]):
            if ch in _CJK_PREFIX_CHARS and long_s[:i] + long_s[i + 1:] == short_s and "一" <= long_s[i + 1] <= "鿿":
                return True
    return False


# 数字表达式整体当一个占位：整数、小数、版本号、范围（26–28、19~22、1.2.3）。上面那条「把每段数字换成 #」
# 只认数字段个数一样的（19–22 / 26–28）；2026-10-23 战役第 1 轮 dry-run 出现「deploy-standard 准则 8 →
# 准则 26–28」，一个是单数、一个是范围，形状不同就漏过去了。
_NUM_EXPR = re.compile(r"\d+(?:\s*[.\-–—~～/至到]\s*\d+)*")


def _differ_only_in_numbers(a: str, b: str) -> bool:
    """去掉数字表达式后两边一样、数字本身不同 → 是不同的东西（准则 8 ≠ 准则 26–28、Python 3.9 ≠ Python 3.13）。"""
    la, lb = (a or "").lower(), (b or "").lower()
    na, nb = _NUM_EXPR.findall(la), _NUM_EXPR.findall(lb)
    # 只比数字本身：26–28 与 26-28 是同一个范围，分隔符写法不同不算数字不同
    if not na or not nb or [re.findall(r"\d+", v) for v in na] == [re.findall(r"\d+", v) for v in nb]:
        return False
    return _lex(_NUM_EXPR.sub("#", la)) == _lex(_NUM_EXPR.sub("#", lb))


def _lexically_same(a: str, b: str, entity_type: str | None = None) -> bool:
    """向量召回的候选必须在词面上也站得住。

    - 去分隔符后完全相等 → 同一对象（大小写/空格/标点差异）。
    - 只在数字上不同（phase1m/phase1p 不算数字，但 19–22 / 26–28、相邻日期算）→ 不是同一对象。
    - 代码类名字（标识符、路径）或 File/Config 类型 → 只认完全相等。
    - 其余：一方包含另一方且短名占长名 ≥ CONTAIN_MIN_SHARE，或 difflib ≥ LEXICAL_MIN_RATIO。
    """
    import difflib
    if entity_type in ("File", "Config") or (_CODE_LIKE.match(a.strip()) and _CODE_LIKE.match(b.strip())):
        # 代码类：分隔符有意义（deploy/nas/release.sh ≠ deploy/nas-release.sh），只认大小写无关的完全相等，
        # 唯一例外是同一文件的「全路径」与「文件名/子路径」（在 / 边界上是后缀）。
        # 第 3 轮复盘：`:` 不能算边界——规则 16 用 `仓库:路径` 区分跨仓库同名文件，
        # `credvault:tests/x.py` 若在 `:` 上放行就会并回 kg-hub 的 `tests/x.py`。
        la, lb = " ".join(a.split()).lower(), " ".join(b.split()).lower()
        if la == lb:
            return True
        # 2026-10-22 三模型对比第 1 轮：`report-portal:tests/test_deployment_standard.py` 并进了不带仓库前缀的
        # `test_deployment_standard.py`（kg-hub 也有同名文件）——冒号后的路径里有 `/`，后缀照样成立。
        # 规则 16 写了仓库前缀的名字，只与同一仓库前缀的名字比较；不带前缀的名字分不清是哪个仓库，不认。
        pa, pb = _repo_prefix(la), _repo_prefix(lb)
        if (pa or pb) and pa != pb:
            return False
        if pa:   # 同一仓库：去掉前缀再按路径比较
            la, lb = la[len(pa) + 1:], lb[len(pb) + 1:]
            if la == lb:
                return True
        short, long_ = (la, lb) if len(la) <= len(lb) else (lb, la)
        if not long_.endswith("/" + short) or not _FILE_TAIL.search(short):
            return False
        # 短名只有一段且是泛用文件名 → 不认（宁可多一个节点）
        return "/" in short or short not in GENERIC_BASENAMES
    x, y = _lex(a), _lex(b)
    if not x or not y:
        return False
    if x == y:
        return True
    if re.sub(r"\d+", "#", x) == re.sub(r"\d+", "#", y) or _differ_only_in_numbers(a, b):
        return False
    if _differ_by_contrast(x, y) or _differ_by_prefix(a, b):
        return False
    short, long_ = (x, y) if len(x) <= len(y) else (y, x)
    if len(short) >= 4 and short in long_ and len(short) / len(long_) >= CONTAIN_MIN_SHARE:
        return True
    return difflib.SequenceMatcher(None, x, y).ratio() >= LEXICAL_MIN_RATIO


def normalize_extraction(entities: list[ExtractedEntity], edges: list[ExtractedEdge],
                         entity_types: set[str], edge_types: set[str],
                         fallback_type: str = "Concept",
                         fallback_edge: str = "relates_to") -> NormalizedExtraction:
    """纯函数：去重、校验、schema 映射，并把纪律指标写进 stats。不读图不写图。"""
    if len(entities) > MAX_ENTITIES:
        raise ValueError(f"entities 超过 {MAX_ENTITIES} 条")
    if len(edges) > MAX_EDGES:
        raise ValueError(f"edges 超过 {MAX_EDGES} 条")
    edge_types_lower = {e.lower() for e in edge_types}
    stats = {"entities_in": len(entities), "edges_in": len(edges),
             "unknown_type": 0, "offschema_edge": 0, "dangling_edge": 0,
             "dup_entity": 0, "dup_edge": 0, "dirty_name": 0, "self_edge": 0}
    out_entities: dict[str, NormalizedEntity] = {}
    for e in entities:
        name = " ".join(e.name.split())
        key = _norm_key(name)
        if not key:
            continue
        if key in out_entities:
            stats["dup_entity"] += 1
            continue
        raw_type = e.type.strip()
        typ = raw_type if raw_type in entity_types else None
        if typ is None:
            # 容忍大小写：tool → Tool
            match = [t for t in entity_types if t.lower() == raw_type.lower()]
            typ = match[0] if match else fallback_type
            if not match:
                stats["unknown_type"] += 1
        if any(p.search(name) for p in DIRTY_NAME_PATTERNS):
            stats["dirty_name"] += 1
        out_entities[key] = NormalizedEntity(key=key, name=name, type=typ,
                                             summary=e.summary.strip(), raw_type=raw_type)
    out_edges: list[NormalizedEdge] = []
    # 去重键带 fact：同一对实体、同一边名、不同 fact 是不同的事实，与写入侧 _FIND_EDGE
    # （按 fact 相等才复用）和 graphiti 一致；只有逐字相同的重复边才算 dup_edge。
    # 人写记忆 memory-v3 给同一条 Lesson→Project 挂多条段首句边，按三元组去重会只剩第一条。
    seen_edges: set[tuple[str, str, str, str]] = set()
    for ed in edges:
        sk, tk = _norm_key(ed.source), _norm_key(ed.target)
        if sk not in out_entities or tk not in out_entities:
            stats["dangling_edge"] += 1
            continue
        if sk == tk:
            stats["self_edge"] += 1
            continue
        raw_name = ed.name.strip()
        lname = raw_name.lower()
        name = lname if lname in edge_types_lower else fallback_edge
        if lname not in edge_types_lower:
            stats["offschema_edge"] += 1
        fact = " ".join(ed.fact.split())
        key = (sk, name, tk, fact)
        if key in seen_edges:
            stats["dup_edge"] += 1
            continue
        seen_edges.add(key)
        out_edges.append(NormalizedEdge(source_key=sk, target_key=tk, name=name,
                                        fact=fact, raw_name=raw_name))
    stats["entities_out"] = len(out_entities)
    stats["edges_out"] = len(out_edges)
    return NormalizedExtraction(entities=list(out_entities.values()), edges=out_edges, stats=stats)


# ---------- 写图（与 graphiti_core 0.29.0 FalkorDB save 语句一致） ----------

_FIND_EXACT = (
    "MATCH (n:Entity) WHERE n.group_id = $g AND toLower(n.name) = $lname "
    "RETURN n.uuid AS uuid, n.name AS name, labels(n) AS labels LIMIT 1")
_FIND_SIMILAR = (
    "MATCH (n:Entity) WHERE n.group_id = $g AND n.name_embedding IS NOT NULL "
    "WITH n, (2 - vec.cosineDistance(n.name_embedding, vecf32($v)))/2 AS score "
    # 第 3 轮复盘：候选顺序要确定，否则 dry-run 与正式提交会并到不同节点；同分按 uuid 定序，
    # 取前 5 个再过词面/类型校验，而不是只看最相似的那一个。
    "WHERE score > $min RETURN n.uuid AS uuid, n.name AS name, labels(n) AS labels, score "
    "ORDER BY score DESC, n.uuid ASC LIMIT 5")
_SAVE_NODE = (
    "MERGE (n:Entity {uuid: $entity_data.uuid}) SET n:{labels} SET n = $entity_data "
    "SET n.name_embedding = vecf32($entity_data.name_embedding) RETURN n.uuid AS uuid")
_SAVE_EPISODE = (
    "MERGE (e:Episodic {uuid: $uuid}) SET e = {uuid: $uuid, name: $name, group_id: $group_id, "
    "source_description: $source_description, source: $source, content: $content, "
    "entity_edges: $entity_edges, created_at: $created_at, valid_at: $valid_at, "
    "extractor_model: $extractor_model, extraction_mode: $extraction_mode, "
    "extractor_session: $extractor_session, prompt_version: $prompt_version, "
    # 2026-10-19 框架 §1：执行工具与实验 run 是独立于模型的两个变量。旧 episode 没有这两个字段，
    # 读侧一律 coalesce 成 null/未知，不回填。
    "extractor_tool: $extractor_tool, run_id: $run_id} "
    "RETURN e.uuid AS uuid")
_SAVE_MENTIONS = (
    "MATCH (episode:Episodic {uuid: $episode_uuid}) MATCH (node:Entity {uuid: $entity_uuid}) "
    "MERGE (episode)-[e:MENTIONS {uuid: $uuid}]->(node) "
    "SET e.group_id = $group_id, e.created_at = $created_at RETURN e.uuid AS uuid")
# 复用条件含 fact 相等：fact 才是被检索的那句话。只按 (源, 边名, 目标) 复用会把新写法的 fact
# 丢掉、还让旧 fact 被新 episode 背书续命——2026-10-19 人写记忆改版（T-0332）实测踩中。
_FIND_EDGE = (
    "MATCH (a:Entity {uuid: $src})-[e:RELATES_TO {name: $name}]->(b:Entity {uuid: $dst}) "
    "WHERE e.group_id = $g AND e.fact = $fact RETURN e.uuid AS uuid, e.episodes AS episodes LIMIT 1")
_APPEND_EPISODE = (
    "MATCH ()-[e:RELATES_TO {uuid: $uuid}]->() "
    "SET e.episodes = CASE WHEN $ep IN coalesce(e.episodes, []) THEN e.episodes "
    "ELSE coalesce(e.episodes, []) + [$ep] END RETURN e.uuid AS uuid")
_SAVE_EDGE = (
    "MATCH (source:Entity {uuid: $edge_data.source_uuid}) "
    "MATCH (target:Entity {uuid: $edge_data.target_uuid}) "
    "MERGE (source)-[e:RELATES_TO {uuid: $edge_data.uuid}]->(target) SET e = $edge_data "
    "SET e.fact_embedding = vecf32($edge_data.fact_embedding) RETURN e.uuid AS uuid")
_LINK_EPISODE_EDGES = (
    "MATCH (e:Episodic {uuid: $uuid}) SET e.entity_edges = $entity_edges")


def _cos_score(u, v) -> float:
    """与 _FIND_SIMILAR 同一口径：(2 - cosineDistance) / 2 = (1 + cos) / 2。"""
    dot = sum(float(a) * float(b) for a, b in zip(u, v))
    nu = sum(float(a) ** 2 for a in u) ** 0.5
    nv = sum(float(b) ** 2 for b in v) ** 0.5
    return (1 + dot / (nu * nv)) / 2 if nu and nv else 0.0


# dry-run 的 pending 名字每条请求都会带上（一批 50 条、累计几百个名字），按 embedder 实例缓存，免得反复嵌入
_PENDING_VEC_CACHE: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
_PENDING_VEC_CACHE_MAX = 5000


async def _embed_cached(embedder, names: list[str]) -> list:
    try:
        cache = _PENDING_VEC_CACHE.setdefault(embedder, {})
    except TypeError:   # 不能弱引用的 embedder：不缓存
        cache = {}
    missing = [n for n in dict.fromkeys(names) if n and n not in cache]
    if missing:
        if len(cache) + len(missing) > _PENDING_VEC_CACHE_MAX:
            cache.clear()
        for n, v in zip(missing, await embedder.create_batch(missing)):
            cache[n] = v
    return [cache.get(n) for n in names]


@dataclass
class WriteResult:
    episode_uuid: str
    nodes_new: int
    nodes_reused: int
    edges_new: int
    edges_reused: int
    resolved: dict  # key -> {"uuid", "reused": bool, "matched_name"}


async def write_extraction(driver, embedder, extraction: NormalizedExtraction, *,
                           name: str, episode_body: str, source_description: str,
                           reference_time: datetime, extractor_model: str,
                           group_id: str, extractor_session: str | None = None,
                           prompt_version: str | None = None,
                           episode_uuid: str | None = None,
                           node_match_min: float = NODE_MATCH_MIN,
                           type_guard: bool = True, dry_run: bool = False,
                           extraction_mode: str = "agent",
                           extractor_tool: str | None = None,
                           run_id: str | None = None,
                           pending: list[dict] | None = None) -> WriteResult:
    """把归一后的抽取写进图。driver 只需 execute_query；embedder 只需 create_batch。

    dry_run=True：只做嵌入与消解，不写任何东西（第 2 轮复盘：让抽取者在写入前看到会并到哪个旧实体）。
    extraction_mode：episode 与新建节点/边上的来源方式标记——'agent'（会话自抽取）或
    'gateway_single'（网关单次抽取，utils/single_pass_extract）；看板与 extractor_compare 按它分组。
    extractor_tool / run_id：来源标记的另外两个维度（执行工具、实验 run），随 episode / 新实体 / 新边落图；
    缺省 None，与旧内容一样在读侧按「未知」处理。
    pending（仅 dry_run）：同一批里排在前面的条目会新建的实体 [{name, type}]。正式提交时它们已经在图里、
    会参与消解，dry-run 不把它们（以及本条前面的实体）算进候选就看不见同批内的误并（2026-10-22 补交
    b0024：同批先建的 `validate 阶段相似度计数` 吃掉了后面的 `prevalidate 阶段相似度计数`，dry-run 没显示）。
    命中它们的实体在 resolved 里 uuid 为 None、in_batch 为 True。"""
    now = datetime.now(tz=timezone.utc)
    now_iso = now.isoformat()
    ep_uuid = episode_uuid or str(uuidlib.uuid4())
    names = [e.name for e in extraction.entities]
    name_vecs = await embedder.create_batch(names) if names else []
    virtual: list[dict] = []   # dry-run 模拟的「已在图里」的实体：同批前面条目新建的 + 本条前面新建的
    if dry_run and pending:
        pnames = [" ".join(str(p.get("name") or "").split()) for p in pending]
        pvecs = await _embed_cached(embedder, pnames)
        virtual = [{"name": n, "key": _norm_key(n), "type": p.get("type"), "vec": v}
                   for n, p, v in zip(pnames, pending, pvecs) if n]
    resolved: dict[str, dict] = {}
    nodes_new = nodes_reused = 0
    for ent, vec in zip(extraction.entities, name_vecs):
        rows, _, _ = await driver.execute_query(_FIND_EXACT, g=group_id, lname=ent.key)
        if not rows and dry_run:
            rows = [{"uuid": None, "name": v["name"], "labels": ["Entity", v["type"]], "in_batch": True}
                    for v in virtual if v["key"] == ent.key][:1]
        if not rows:
            rows, _, _ = await driver.execute_query(
                _FIND_SIMILAR, g=group_id, v=[float(x) for x in vec], min=node_match_min)
            if dry_run and virtual:
                rows = sorted(list(rows) + [
                    {"uuid": None, "name": v["name"], "labels": ["Entity", v["type"]], "score": s, "in_batch": True}
                    for v in virtual if (s := _cos_score(vec, v["vec"])) > node_match_min],
                    key=lambda r: -float(r.get("score") or 0))[:5]
            # 第 1 轮（haiku）复盘：按名字近似合并不看类型，把 Issue 并进了 Config、Tool 并进了 Issue、
            # 文件并进了同名不同路径的另一个文件。向量候选还要带抽出的类型标签才复用。
            rows = [r for r in rows
                    if _lexically_same(ent.name, r.get("name") or "", ent.type)
                    and (not type_guard or ent.type in (r.get("labels") or []))]
        if rows:
            resolved[ent.key] = {"uuid": rows[0]["uuid"], "reused": True,
                                 "matched_name": rows[0].get("name")}
            if rows[0].get("in_batch"):
                resolved[ent.key]["in_batch"] = True
            nodes_reused += 1
            continue
        new_uuid = str(uuidlib.uuid4())
        if dry_run:
            resolved[ent.key] = {"uuid": None, "reused": False, "matched_name": None}
            virtual.append({"name": ent.name, "key": ent.key, "type": ent.type, "vec": vec})
            nodes_new += 1
            continue
        entity_data = {
            "uuid": new_uuid, "name": ent.name, "name_embedding": [float(x) for x in vec],
            "group_id": group_id, "summary": ent.summary, "created_at": now_iso,
            "extractor_model": extractor_model, "extraction_mode": extraction_mode,
            "extractor_tool": extractor_tool, "run_id": run_id,
        }
        await driver.execute_query(_SAVE_NODE.replace("{labels}", f"{ent.type}:Entity"),
                                   entity_data=entity_data)
        resolved[ent.key] = {"uuid": new_uuid, "reused": False, "matched_name": None}
        nodes_new += 1

    if dry_run:
        return WriteResult(episode_uuid="", nodes_new=nodes_new, nodes_reused=nodes_reused,
                           edges_new=0, edges_reused=0, resolved=resolved)
    await driver.execute_query(
        _SAVE_EPISODE, uuid=ep_uuid, name=name, group_id=group_id,
        source_description=source_description, source="text", content=episode_body,
        entity_edges=[], created_at=now_iso, valid_at=reference_time.isoformat(),
        extractor_model=extractor_model, extraction_mode=extraction_mode,
        extractor_session=extractor_session, prompt_version=prompt_version,
        extractor_tool=extractor_tool, run_id=run_id)
    for ent in extraction.entities:
        await driver.execute_query(
            _SAVE_MENTIONS, episode_uuid=ep_uuid, entity_uuid=resolved[ent.key]["uuid"],
            uuid=str(uuidlib.uuid4()), group_id=group_id, created_at=now_iso)

    facts = [e.fact for e in extraction.edges]
    fact_vecs = await embedder.create_batch(facts) if facts else []
    edge_uuids: list[str] = []
    edges_new = edges_reused = 0
    for ed, vec in zip(extraction.edges, fact_vecs):
        src = resolved[ed.source_key]["uuid"]
        dst = resolved[ed.target_key]["uuid"]
        rows, _, _ = await driver.execute_query(_FIND_EDGE, src=src, dst=dst, name=ed.name, g=group_id,
                                                fact=ed.fact)
        if rows:
            await driver.execute_query(_APPEND_EPISODE, uuid=rows[0]["uuid"], ep=ep_uuid)
            edge_uuids.append(rows[0]["uuid"])
            edges_reused += 1
            continue
        edge_uuid = str(uuidlib.uuid4())
        edge_data = {
            "source_uuid": src, "target_uuid": dst, "uuid": edge_uuid, "name": ed.name,
            "group_id": group_id, "fact": ed.fact, "fact_embedding": [float(x) for x in vec],
            "episodes": [ep_uuid], "created_at": now_iso, "expired_at": None,
            "valid_at": reference_time.isoformat(), "invalid_at": None,
            "reference_time": reference_time.isoformat(),
            "extractor_model": extractor_model, "extraction_mode": extraction_mode,
            "raw_name": ed.raw_name, "extractor_tool": extractor_tool, "run_id": run_id,
        }
        await driver.execute_query(_SAVE_EDGE, edge_data=edge_data)
        edge_uuids.append(edge_uuid)
        edges_new += 1
    if edge_uuids:
        await driver.execute_query(_LINK_EPISODE_EDGES, uuid=ep_uuid, entity_edges=edge_uuids)
    return WriteResult(episode_uuid=ep_uuid, nodes_new=nodes_new, nodes_reused=nodes_reused,
                       edges_new=edges_new, edges_reused=edges_reused, resolved=resolved)


_CHECK_KEYS = (
    "UNWIND $items AS it MATCH (k:IngestedKey {source_description: it.sd, source_obs_id: it.sid}) "
    "RETURN it.sd AS sd, it.sid AS sid, k.status AS status, k.episode_uuid AS episode_uuid")


_CHECK_NAMES = ("MATCH (k:IngestedKey) WHERE k.name IN $names "
                "RETURN k.name AS name, k.status AS status")


async def check_names(driver, names: list[str]) -> dict[str, str]:
    """按 episode 名批量查 IngestedKey 状态。refinery 用它把别的路径（会话自抽取）已入库的
    观测记为完成——否则断路器断开期间它一条都不提交，水位永远不知道这些观测已经进图。"""
    if not names:
        return {}
    rows, _, _ = await driver.execute_query(_CHECK_NAMES, names=list(names))
    return {r["name"]: r.get("status") for r in rows if r.get("name")}


async def check_keys(driver, items: list[dict]) -> dict[tuple[str, str], dict]:
    """批量查 IngestedKey 状态，供执行者跳过已入库的观测。"""
    if not items:
        return {}
    rows, _, _ = await driver.execute_query(
        _CHECK_KEYS, items=[{"sd": i["source_description"], "sid": i["source_obs_id"]} for i in items])
    return {(r["sd"], r["sid"]): {"status": r.get("status"), "episode_uuid": r.get("episode_uuid")}
            for r in rows}
