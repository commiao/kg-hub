"""
kg-hub schema v0.2 — Pydantic models for Graphiti entity_types / edge_types.

Purpose: solve the SPIKE-observed problem that LLM coins its own edge names
(PROPOSES_SOLUTION, MISROUTED_TO, ...) instead of using our canonical schema.

Reference: DESIGN.md §4 (node + edge tables).

Usage from ingester:
    from schema import ENTITY_TYPES, EDGE_TYPES, EDGE_TYPE_MAP
    await g.add_episode(
        ...,
        entity_types=ENTITY_TYPES,
        edge_types=EDGE_TYPES,
        edge_type_map=EDGE_TYPE_MAP,
    )
"""

from pydantic import BaseModel, Field


# ----- Entity types (v0.2, 13 types) -----
class Person(BaseModel):
    """A human actor. Examples: jingmiao@liblib.ai."""
    org: str | None = Field(None, description="Organization affiliation, e.g. liblib")


class Project(BaseModel):
    """A code project / repository. Examples: claude-mem, kg-hub, OpenClaw."""
    path: str | None = None
    repo: str | None = None


class File(BaseModel):
    """A specific file. Examples: HANDOVER.md, worker-service.cjs, notify-send.sh."""
    path: str | None = None
    project_id: str | None = None


class Tool(BaseModel):
    """A piece of software / service. Examples: claude-mem worker, cc-switch, launchd, Cron."""
    category: str | None = None
    version: str | None = None


class Concept(BaseModel):
    """An abstract idea / principle / rule. Examples: qwen3.6-plus model, 银行账单记账原则, FTS5."""
    description: str | None = None


class Issue(BaseModel):
    """A problem / bug / failure that occurred. Examples: search 30s timeout, Cron 通知发送失败."""
    severity: str | None = None
    status: str | None = None


class Fix(BaseModel):
    """A concrete remedy applied to an Issue. Examples: notify-send.sh, CLAUDE_MEM_CHROMA_ENABLED=false."""
    applied_at: str | None = None


class Config(BaseModel):
    """A configuration artifact. Examples: ~/.claude-mem/.env, com.claude-mem.worker.plist."""
    path: str | None = None
    content_hash: str | None = None


class Session(BaseModel):
    """A single agent / chat session. Examples: claude-mem session, OpenClaw feishu session."""
    started_at: str | None = None
    platform: str | None = None


class Observation(BaseModel):
    """A raw observation captured by claude-mem from a tool call."""
    obs_id: str | None = None
    source_device: str | None = None


class Capsule(BaseModel):
    """An OpenClaw knowledge capsule (refined markdown). Examples: CAPSULE-NOTIFICATION-ROUTE-2026."""
    title: str | None = None
    capsule_type: str | None = Field(None, description="架构设计 / 问题诊断 / 最佳实践 / 运维恢复")
    tags: str | None = None
    # quality_rating / usage_count come from OpenClaw markdown as raw strings
    # (e.g. "5.0/5", "3 次", "None"). Keep them as str to avoid Pydantic
    # strict-mode parse errors from LLM-returned 'None'.
    quality_rating: str | None = None
    usage_count: str | None = None
    source_session: str | None = None


class KnowledgeDoc(BaseModel):
    """A reference document. Examples: feishu-image-upload-complete-guide.md."""
    filename: str | None = None
    category: str | None = None
    path: str | None = None


class Lesson(BaseModel):
    """A distilled learning / principle. Examples: 银行数据视为 100% 正确."""
    statement: str | None = Field(None, description="One-sentence statement of the principle")
    derived_at: str | None = None


ENTITY_TYPES: dict[str, type[BaseModel]] = {
    "Person": Person,
    "Project": Project,
    "File": File,
    "Tool": Tool,
    "Concept": Concept,
    "Issue": Issue,
    "Fix": Fix,
    "Config": Config,
    "Session": Session,
    "Observation": Observation,
    "Capsule": Capsule,
    "KnowledgeDoc": KnowledgeDoc,
    "Lesson": Lesson,
}


# ----- Edge types (v0.2, 15 types) -----
# Each model's docstring is the prompt the LLM sees when deciding to use this edge.
class caused_by(BaseModel):
    """An Issue's root cause is a Concept or another Issue. Example: search timeout caused_by ChromaDB cold start."""


class fixed_by(BaseModel):
    """An Issue is resolved by a specific Fix. Example: search timeout fixed_by env CHROMA_ENABLED=false."""


class verified_by(BaseModel):
    """A Fix is confirmed working by an Observation or Capsule. Example: notify-send fix verified_by 2026-03-20 drill."""


class references(BaseModel):
    """An Observation references a File or Concept (mentions it)."""


class modified(BaseModel):
    """An Observation describes that a File was modified."""


class depends_on(BaseModel):
    """A Tool depends on another Tool or Config. Example: claude-mem worker depends_on launchd plist."""


class supersedes(BaseModel):
    """A newer Fix replaces an older Fix doing the same job."""


class derived_from(BaseModel):
    """A Concept or Lesson was distilled from an Observation or Session."""


class occurred_in(BaseModel):
    """An Observation happened during a Session."""


class belongs_to(BaseModel):
    """A Session belongs to a Project (or a File belongs to a Project)."""


class extracted_from(BaseModel):
    """A Capsule was extracted/refined from a specific Session."""


class relates_to(BaseModel):
    """Two Capsules are semantically related (same topic / shared context)."""


class documented_in(BaseModel):
    """A Concept is fully documented in a KnowledgeDoc."""


class diagnosed_by(BaseModel):
    """An Issue was diagnosed / analyzed in a specific Capsule."""


class implemented_as(BaseModel):
    """A Capsule's proposed solution was implemented as a concrete Fix (often involving Files / Tools)."""


EDGE_TYPES: dict[str, type[BaseModel]] = {
    "caused_by": caused_by,
    "fixed_by": fixed_by,
    "verified_by": verified_by,
    "references": references,
    "modified": modified,
    "depends_on": depends_on,
    "supersedes": supersedes,
    "derived_from": derived_from,
    "occurred_in": occurred_in,
    "belongs_to": belongs_to,
    "extracted_from": extracted_from,
    "relates_to": relates_to,
    "documented_in": documented_in,
    "diagnosed_by": diagnosed_by,
    "implemented_as": implemented_as,
}


# ----- Edge type map: which edges are valid between which node-type pairs -----
# Tuple key: (from_node_type, to_node_type) → list of allowed edge type names.
# "Entity" is Graphiti's catch-all when a node hasn't been narrowed to a custom type yet.
EDGE_TYPE_MAP: dict[tuple[str, str], list[str]] = {
    ("Issue", "Concept"): ["caused_by"],
    ("Issue", "Issue"): ["caused_by"],
    ("Issue", "Fix"): ["fixed_by"],
    ("Issue", "Capsule"): ["diagnosed_by"],
    ("Fix", "Observation"): ["verified_by"],
    ("Fix", "Capsule"): ["verified_by"],
    ("Fix", "Fix"): ["supersedes"],
    ("Capsule", "Fix"): ["implemented_as"],
    ("Capsule", "Session"): ["extracted_from"],
    ("Capsule", "Capsule"): ["relates_to"],
    ("Concept", "KnowledgeDoc"): ["documented_in"],
    ("Concept", "Observation"): ["derived_from"],
    ("Lesson", "Issue"): ["derived_from"],
    ("Tool", "Tool"): ["depends_on"],
    ("Tool", "Config"): ["depends_on"],
    ("Observation", "File"): ["references", "modified"],
    ("Observation", "Concept"): ["references"],
    ("Observation", "Session"): ["occurred_in"],
    ("Session", "Project"): ["belongs_to"],
    ("File", "Project"): ["belongs_to"],
    # catch-all so Entity↔Entity links don't blow up before LLM narrows types
    ("Entity", "Entity"): list(EDGE_TYPES.keys()),
}
