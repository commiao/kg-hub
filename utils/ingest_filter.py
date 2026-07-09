"""
kg-hub ingest filter — quality gate for claude-mem → kg-hub.

Pure-Python, deterministic, zero LLM calls. Loaded by
ingesters/claude_mem_obs.py before invoking graphiti.add_episode().

Two outputs per observation:
  Decision(accept=bool, score=float, reasons=list[str])
  + an append-only JSONL log entry at data/.ingest_decisions.jsonl

Defaults to shadow_mode=true: decisions are computed and logged, but
accept=True regardless. Flip shadow_mode to false in
config/ingest_filter.json only after reviewing logs.

The filter is layered:
  Layer 1: hard gates (type / narrative / facts / model)
  Layer 2: weighted score vs per-platform threshold
  Layer 3: per-(platform, project) daily quota
Type overrides allow decision/bugfix/security_alert to bypass thresholds
and quotas — these are never rejected.
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .ops_noise import is_ops_noise


CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "ingest_filter.json"
DECISIONS_LOG = Path(__file__).resolve().parent.parent / "data" / ".ingest_decisions.jsonl"


# ---------- Decision result ----------

@dataclass
class Decision:
    accept: bool                      # whether to actually ingest (respects shadow_mode)
    would_accept: bool                # what the filter would decide ignoring shadow_mode
    score: float
    threshold: float
    reasons: list[str] = field(default_factory=list)
    layer: str = ""                   # which layer ruled: "hard_gate" / "score" / "quota" / "override" / "pass"
    obs_id: int = 0
    obs_type: str = ""
    platform: str = ""
    project: str = ""
    shadow_mode: bool = False


# ---------- Config loader (re-read every run so edits hot-reload) ----------

def load_config(path: Path = CONFIG_PATH) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"ingest filter config missing: {path}")
    return json.loads(path.read_text())


# ---------- Helpers ----------

def _decode_json_list(field_val) -> list:
    if not field_val:
        return []
    if isinstance(field_val, list):
        return field_val
    try:
        v = json.loads(field_val)
        return v if isinstance(v, list) else []
    except Exception:
        return []


def _today_utc() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")


# ---------- Scoring ----------

def compute_score(obs: dict, scoring_cfg: dict) -> tuple[float, dict]:
    """Pure scoring. Returns (score, breakdown_dict)."""
    type_ = obs.get("type") or ""
    type_weight = scoring_cfg["type_weight"].get(type_, 0)

    narrative = obs.get("narrative") or ""
    narrative_score = min(len(narrative), scoring_cfg["narrative_chars_cap"]) / scoring_cfg["narrative_chars_divisor"]

    facts = _decode_json_list(obs.get("facts"))
    facts_score = min(len(facts), scoring_cfg["facts_cap"]) * scoring_cfg["facts_weight"]

    concepts = _decode_json_list(obs.get("concepts"))
    concepts_score = min(len(concepts), scoring_cfg["concepts_cap"]) * scoring_cfg["concepts_weight"]

    files_modified = _decode_json_list(obs.get("files_modified"))
    files_score = len(files_modified) * scoring_cfg["files_modified_weight"]

    relevance = int(obs.get("relevance_count") or 0)
    relevance_score = min(relevance, scoring_cfg["relevance_cap"]) * scoring_cfg["relevance_weight"]

    total = round(
        type_weight + narrative_score + facts_score + concepts_score
        + files_score + relevance_score,
        2,
    )
    return total, {
        "type": round(type_weight, 2),
        "narrative": round(narrative_score, 2),
        "facts": round(facts_score, 2),
        "concepts": round(concepts_score, 2),
        "files": round(files_score, 2),
        "relevance": round(relevance_score, 2),
    }


# ---------- Daily quota tracker (per process) ----------
# Kept in-memory for the duration of a single ingester run, which is fine
# because launchd kicks one run every 15 min with --limit 10, so quota
# enforcement within a single run is the meaningful scope. For multi-run
# quota tracking we'd need persistent state — deliberately deferred.

class QuotaTracker:
    def __init__(self) -> None:
        self._counts: dict[tuple[str, str, str], int] = defaultdict(int)

    def used(self, platform: str, project: str, type_: str) -> int:
        return self._counts[(platform, project, type_)]

    def consume(self, platform: str, project: str, type_: str) -> None:
        self._counts[(platform, project, type_)] += 1


# ---------- The filter ----------

def evaluate(
    obs: dict,
    cfg: dict,
    quotas: Optional[QuotaTracker] = None,
) -> Decision:
    """
    Run all three layers; return a Decision.

    obs is the row dict from claude-mem.db, augmented with:
      - platform_source (str or None)
      - relevance_count (int)
      - generated_by_model (str or None)
    """
    type_ = obs.get("type") or ""
    platform = (obs.get("platform_source") or "_default") or "_default"
    project = obs.get("project") or "(unknown)"
    shadow = bool(cfg.get("shadow_mode", True))

    g = cfg["global"]
    scoring_cfg = cfg["scoring"]
    plat_cfg = cfg["platforms"].get(platform) or cfg["platforms"]["_default"]
    overrides = cfg.get("type_overrides", {}).get(type_, {})

    reasons: list[str] = []

    # --- Layer 1: hard gates (cannot be overridden) ---
    if type_ not in g["type_whitelist"]:
        reasons.append(f"type='{type_}' not in whitelist")
        return _build(False, False, 0.0, 0.0, reasons, "hard_gate",
                      obs, type_, platform, project, shadow)

    narrative = obs.get("narrative") or ""
    if len(narrative) < g["min_narrative_chars"]:
        reasons.append(
            f"narrative too short ({len(narrative)} < {g['min_narrative_chars']})"
        )
        # narrative gate can be bypassed only for security_alert (catastrophic events
        # may have short narratives); decision/bugfix still need substance
        if type_ != "security_alert":
            return _build(False, False, 0.0, 0.0, reasons, "hard_gate",
                          obs, type_, platform, project, shadow)

    facts = _decode_json_list(obs.get("facts"))
    files_modified = _decode_json_list(obs.get("files_modified"))
    # Substantial narrative compensates for missing structured facts/files —
    # avoids rejecting long-form bugfix/decision obs where the writer poured
    # the content into prose instead of structured facts (e.g. obs #429).
    substantive_narrative_bypass = (
        len(narrative) >= g.get("substantive_narrative_chars", 300)
    )
    if (
        len(facts) + len(files_modified) < g["min_facts_or_files"]
        and not substantive_narrative_bypass
    ):
        reasons.append(f"insufficient facts+files ({len(facts)}+{len(files_modified)})")
        if type_ != "security_alert":
            return _build(False, False, 0.0, 0.0, reasons, "hard_gate",
                          obs, type_, platform, project, shadow)

    model = (obs.get("generated_by_model") or "").lower()
    if any(bad in model for bad in g["reject_models"]) and model != "":
        reasons.append(f"rejected model: {model}")
        return _build(False, False, 0.0, 0.0, reasons, "hard_gate",
                      obs, type_, platform, project, shadow)

    # --- Compute score regardless (always useful in log) ---
    score, breakdown = compute_score(obs, scoring_cfg)
    threshold = float(plat_cfg["score_threshold"])

    # --- ops_noise 专项闸：kg-hub 自身运维自指 bugfix（WS-3）---
    # 武装开关在消费端：enabled=false 时完全短路，行为与今日一致（分类器仍可被体检器独立调用）。
    # 命中则：扣分 + 抬门槛(min_score) + 剥夺 type_override 豁免——刻意放在 override 分支之前，
    # 使 bugfix 无法借 bypass 逃逸。仅极少数超高分运维记录能翻身（逃逸阀）。
    if bool(cfg.get("ops_noise", {}).get("enabled", False)) and is_ops_noise(obs, cfg):
        oc = cfg["ops_noise"]
        score = round(score - float(oc.get("score_penalty", 100)), 2)
        eff_threshold = max(threshold, float(oc.get("min_score", 120)))
        accepted = score >= eff_threshold
        reasons.append("ops_noise: penalized & override revoked")
        reasons.append(f"ops_noise score {score} {'>=' if accepted else '<'} {eff_threshold}")
        return _build(accepted, accepted, score, eff_threshold, reasons, "ops_noise",
                      obs, type_, platform, project, shadow, breakdown=breakdown)

    # --- Type override: bypass threshold (decision/bugfix/security_alert) ---
    if overrides.get("bypass_threshold"):
        reasons.append(f"type override: {type_} bypasses threshold")
        # but still maybe respect quota
        if not overrides.get("bypass_quota") and quotas is not None:
            quota = int(plat_cfg.get("daily_quota_per_project") or 0)
            used = quotas.used(platform, project, type_)
            if quota and used >= quota:
                reasons.append(f"quota exhausted (used={used}, quota={quota})")
                return _build(False, False, score, threshold, reasons,
                              "quota", obs, type_, platform, project, shadow,
                              breakdown=breakdown)
            if quotas is not None:
                quotas.consume(platform, project, type_)
        return _build(True, True, score, threshold, reasons, "override",
                      obs, type_, platform, project, shadow, breakdown=breakdown)

    # --- Layer 2: score vs threshold ---
    if score < threshold:
        reasons.append(f"score {score} < threshold {threshold} (platform={platform})")
        return _build(False, False, score, threshold, reasons, "score",
                      obs, type_, platform, project, shadow, breakdown=breakdown)

    # --- Layer 3: quota ---
    if quotas is not None and not overrides.get("bypass_quota"):
        quota = int(plat_cfg.get("daily_quota_per_project") or 0)
        used = quotas.used(platform, project, type_)
        if quota and used >= quota:
            reasons.append(f"quota exhausted (used={used}, quota={quota})")
            return _build(False, False, score, threshold, reasons, "quota",
                          obs, type_, platform, project, shadow, breakdown=breakdown)
        quotas.consume(platform, project, type_)

    reasons.append(f"score {score} >= threshold {threshold}")
    return _build(True, True, score, threshold, reasons, "pass",
                  obs, type_, platform, project, shadow, breakdown=breakdown)


def _build(
    would_accept: bool,
    would_accept_inner: bool,
    score: float,
    threshold: float,
    reasons: list[str],
    layer: str,
    obs: dict,
    type_: str,
    platform: str,
    project: str,
    shadow: bool,
    breakdown: Optional[dict] = None,
) -> Decision:
    # In shadow mode, accept is forced True regardless of would_accept.
    accept = True if shadow else would_accept
    d = Decision(
        accept=accept,
        would_accept=would_accept,
        score=score,
        threshold=threshold,
        reasons=reasons,
        layer=layer,
        obs_id=int(obs.get("id") or 0),
        obs_type=type_,
        platform=platform,
        project=project,
        shadow_mode=shadow,
    )
    # attach breakdown via attribute, not on dataclass (kept flexible)
    if breakdown is not None:
        d._breakdown = breakdown  # type: ignore[attr-defined]
    return d


# ---------- Decision logging ----------

def log_decision(decision: Decision, log_path: Path = DECISIONS_LOG) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now(tz=timezone.utc).isoformat(),
        **asdict(decision),
    }
    breakdown = getattr(decision, "_breakdown", None)
    if breakdown:
        record["score_breakdown"] = breakdown
    with log_path.open("a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------- Summary helper for run-end report ----------

def summarize_decisions(decisions: list[Decision]) -> dict:
    if not decisions:
        return {"n": 0}
    n = len(decisions)
    n_accept = sum(1 for d in decisions if d.would_accept)
    n_reject = n - n_accept
    by_layer: dict[str, int] = defaultdict(int)
    by_platform_decision: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for d in decisions:
        by_layer[d.layer] += 1
        outcome = "accept" if d.would_accept else "reject"
        by_platform_decision[d.platform][outcome] += 1
    return {
        "n": n,
        "would_accept": n_accept,
        "would_reject": n_reject,
        "reject_rate_pct": round(100 * n_reject / max(n, 1), 1),
        "by_layer": dict(by_layer),
        "by_platform": {k: dict(v) for k, v in by_platform_decision.items()},
    }
