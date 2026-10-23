"""观测分诊（obs triage）：在入库闸（utils/ingest_filter）之上，用规则把每条观测分成 keep / review / drop 三档。

为什么要有它（2026-10-24，30 天 223 条分层盲标样本，H/M/L = 69/79/75）：
  现行闸放行 80%，其中低价值（L）被放行 51/75——几乎全是「过程/状态记录」：PR 合了、发布完成、分支领先落后、
  合并请求已建、测试失败清单、例行的加枚举删属性；而被挡的高价值（H）6 条里 5 条只差 5–14 分，正文里都写着
  根因、约定、取舍——闸按类型和长度打分，看不见这两种差别。LLM 能看见，但 token 要花在刀刃上，
  所以这里先用规则：命中过程/状态/会话内临时状态的扣分，命中「根因/约定/取舍/推翻」的加分；
  规则看不准的落在 review 档，将来只对这一档用最便宜的模型复核。

设计钉子：
  - 纯函数、零 I/O、零 LLM；权重全在 config/obs_triage.json，规则正文在本文件（每条有名字、版本、说明）。
  - 不替代入库闸：先 evaluate 得基础分，再在其上加减。闸硬拒（类型不在白名单、模型黑名单、正文过短且无 facts）
    的不救；只救「分数差一点」（layer == "score"）且加分后够线的——与 #237 中文折算救人的口径一致。
  - decision / security_* 永不落 drop（最多 review）：沿用 ops_noise 的钉子「decision/security 零误杀」。
  - enabled=false 时只打标不改判（影子模式）；消费端（agent_extract.select_batch）按 tier 统计，
    enabled=true 才把 drop 档跳过。每条规则怎么验、改了要跑什么，见 docs/OBSERVATION-TRIAGE.md。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .ingest_filter import Decision, evaluate, normalize_type

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "obs_triage.json"
TIERS = ("keep", "review", "drop")


def load_triage_config(path: Path = CONFIG_PATH) -> dict:
    return json.loads(path.read_text())


# ---------- 规则 ----------
# 每条规则：name / kind（penalty 扣分、bonus 加分）/ since（哪一版加的）/ doc（判什么、为什么）/ 匹配函数。
# 正则分英文和中文两段，英文在小写文本上匹配。加新规则：写在这里 + 在 config 里给权重 + 跑 obs_bench score 看三档变化。

@dataclass(frozen=True)
class Rule:
    name: str
    kind: str
    since: str
    doc: str
    en: re.Pattern | None = None
    zh: re.Pattern | None = None
    scope: str = "title"          # title：只看标题+副标题；text：看标题+正文+facts

    def matches(self, title: str, text: str) -> list[str]:
        """命中的原文片段（调规则时看它，别猜是哪个词打中的）。"""
        t = title if self.scope == "title" else text
        out = []
        if self.en:
            out += [m.group(0) for m in self.en.finditer(t.lower())]
        if self.zh:
            out += [m.group(0) for m in self.zh.finditer(t)]
        return out

    def hits(self, title: str, text: str) -> int:
        return len(self.matches(title, text))


def _en(p: str) -> re.Pattern:
    return re.compile(p, re.I)


RULES: tuple[Rule, ...] = (
    Rule("status_report", "penalty", "triage-v1",
         "发布/合并/推送/分支领先落后/健康检查之类的状态快照：当时为真、下一次发布就过时，查 git 和看板比查图快。",
         _en(r"\b(prs? ?#?\d* (already )?merged|merged into (main|master)|already merged|merge request (created|\d+ created)|"
             r"version bumped|pushed to (origin|remote)|branch(es)? (deleted|cleanup|released)|rollout (already )?complete|"
             r"health ?= ?\d+|healthy with|confirmed .{0,40}at commit|(ahead|behind) (of )?origin|sits level|"
             r"is (ahead|behind)|released to image|deployed (at|to) \w+|deploy(ment)? (complete|status)|"
             r"(is|are) (clean and )?mergeable)\b"),
         re.compile(r"(发布完成|已合并|已部署|部署完成|推送(成功|完成)|版本号|分支.{0,6}(落后|领先)|线上版本核对)")),
    Rule("process_record", "penalty", "triage-v1",
         "任务开始/暂停/计划已定/合并请求已建/提交成功这类过程记录：记录的是「正在做」，没有结论。",
         _en(r"\b(plan established|task opened|work paused|paused pending|awaiting user|investigation plan|"
             r"submitted successfully|created on codeup|pull request opened|prs? opened|coordination|"
             r"progress update|step \d+ of \d+|in progress)\b"),
         re.compile(r"(排查计划|任务暂停|调度协调|等待用户|计划推进|创建合并请求|流程调度)")),
    Rule("session_ephemeral", "penalty", "triage-v1",
         "只在那个会话里成立的状态：沙箱禁了什么、网络当时不通、浏览器开了哪些标签、回放了哪条消息。",
         _en(r"\b(sandbox denies|no fresh|unreachable (in|from) (this|the) session|environment inventory|"
             r"browser tabs?|one chrome|multiple monitors|retrieved from (the )?(graph|kg|knowledge graph)|"
             r"replay message|recorded manual|retrospective recovery|enqueued|inventory:)\b",
             ),
         re.compile(r"(沙箱禁|会话内.{0,4}不通|标签清单|回放记录)"),
         scope="text"),
    Rule("test_snapshot", "penalty", "triage-v1",
         "某次测试跑完的失败清单、变异测试覆盖结果、为配合实现改断言/mock：下一次提交就变。",
         _en(r"\b(test suite status|test failures?|failures? identified|mutation testing confirms|"
             r"tests? (corrected|adjusted|updated) to|fixed test|test (mock|fixture|assertion)s?|"
             r"assertion (adjusted|corrected|changed))\b"),
         re.compile(r"(测试失败|断言(调整|收紧)|mock 调整|变异测试)")),
    Rule("routine_code_change", "penalty", "triage-v1",
         "加个枚举、删个过期属性、透传一个参数、go mod tidy：代码本身可读，图里不需要第二份。",
         _en(r"^(added|removed|renamed|deleted|pruned|extended|reduced|introduced|dropped)\b.{0,80}"
             r"(\b(enum|attribute|method|dependency|dependencies|script|flag|field|stale|unused|helper|parameter|arg|"
             r"test suite|tests?|fixture)s?\b|\w+enum\b)"
             r"|\bgo mod tidy\b|\bpasses? \w+ into\b|\bpass-?through\b"),
         re.compile(r"(新增枚举|删除过期|参数透传|依赖精简)")),
    Rule("snapshot_diff", "penalty", "triage-v1",
         "差异行数、待提交清单、探针跑了一次的结果、验收通过并出了报告：是某一刻的快照，不是可复用的结论。",
         _en(r"\b(undeployed delta|pending (changes|diff)|diff (scopes|summary)|\d+ added ?/ ?\d+ (deleted|removed)|"
             r"history check|probe (confirms|returned|run)|verification (complete|passed|and report)|snapshot|"
             r"delta vs|release infrastructure and)\b"),
         re.compile(r"(差异(行数|统计)|待提交|快照|验收(通过|完成))")),
    Rule("rationale", "bonus", "triage-v1",
         "根因、因为、取舍、约定、语义、不要、坑、推翻、实测证明：这些词出现在正文里，说明记的是「为什么」而不是「做了什么」。",
         _en(r"\b(root cause|because|caused by|traced to|due to|trade-?offs?|must not|never|gotcha|pitfalls?|"
             r"proves?|no .{0,20}benefit|rationale|overturn(s|ed)?|refute[sd]?|contradict(s|ed)?|despite|"
             r"turns out|in fact|only when|unless|fails? (when|because|if)|does not (work|apply|hold)|"
             r"not .{0,12}(sufficient|enough|reliable|exact|precise))\b"),
         re.compile(r"(根因|因为|取舍|约定|协议|语义|不要|不能|坑|推翻|纠正|不变量|约束|实测|证明|无效|导致|除非|只有)"),
         scope="text"),
)

RULES_BY_NAME = {r.name: r for r in RULES}


def rules_version(cfg: dict) -> str:
    return str(cfg.get("version", "triage-v0"))


def _decode_list(v) -> list:
    if not v:
        return []
    if isinstance(v, list):
        return v
    try:
        x = json.loads(v)
        return x if isinstance(x, list) else []
    except Exception:
        return []


def obs_texts(obs: dict, narrative_chars: int = 800) -> tuple[str, str]:
    """(标题段, 全文段)。全文只取正文前 narrative_chars 字：长正文后面多是过程细节，不该靠它凑加分。"""
    title = " ".join(x for x in ((obs.get("title") or ""), (obs.get("subtitle") or "")) if x)
    facts = " ".join(str(f) for f in _decode_list(obs.get("facts")))
    text = " ".join(x for x in (title, (obs.get("narrative") or "")[:narrative_chars], facts) if x)
    return title, text


@dataclass
class Verdict:
    tier: str
    base_score: float
    score: float
    threshold: float
    hits: dict[str, int] = field(default_factory=dict)
    adjust: float = 0.0
    reason: str = ""
    split: str = ""
    base: Decision | None = None

    def as_dict(self) -> dict:
        return {"tier": self.tier, "base_score": self.base_score, "score": self.score, "threshold": self.threshold,
                "hits": self.hits, "adjust": self.adjust, "reason": self.reason,
                "base_accept": bool(self.base.would_accept) if self.base else None,
                "base_layer": self.base.layer if self.base else None}


def rule_hits(obs: dict, cfg: dict) -> dict[str, int]:
    """每条启用规则的命中次数（0 不出现）。"""
    title, text = obs_texts(obs, int(cfg.get("narrative_chars", 800)))
    out = {}
    for r in RULES:
        rc = (cfg.get("rules") or {}).get(r.name)
        if not rc or float(rc.get("weight", 0)) == 0:
            continue
        n = r.hits(title, text)
        if n:
            out[r.name] = n
    return out


def type_excess(obs: dict, filter_cfg: dict) -> float:
    """这条观测的类型权重比 change 多出的分。被判为过程记录时把它扣掉：
    「加了个枚举」被标成 feature 就多拿 65 分，而它和一条 change 没区别。"""
    tw = ((filter_cfg.get("scoring") or {}).get("type_weight") or {})
    return max(0.0, float(tw.get(normalize_type(obs.get("type")), 0)) - float(tw.get("change", 0)))


def adjustment(hits: dict[str, int], cfg: dict, type_excess_pts: float = 0.0) -> float:
    """扣分每条规则只算一次（命中几次都是同一类记录）；加分按命中次数封顶；
    正文里「为什么」写得够多（rationale 命中 ≥ penalty_waived_at_rationale_hits）时不扣分——
    标题像状态报告、正文讲清根因的，是知识不是状态。"""
    rules = cfg.get("rules") or {}
    adj = 0.0
    waive = int(cfg.get("penalty_waived_at_rationale_hits", 0) or 0)
    waived = waive and hits.get("rationale", 0) >= waive
    penalized = False
    for name, n in hits.items():
        r, rc = RULES_BY_NAME[name], rules.get(name) or {}
        w = float(rc.get("weight", 0))
        if r.kind == "penalty":
            if not waived:
                adj -= w
                penalized = True
        else:
            adj += w * min(n, int(rc.get("cap", 1)))
    if penalized and cfg.get("neutralize_type_on_penalty", True):
        adj -= type_excess_pts
    return round(adj, 2)


def base_from_record(rec: dict, filter_cfg: dict) -> Decision:
    """冻结样本里记录的闸判定（score / accept / why）还原成 Decision：样本正文是截断过的，重新 evaluate 分数会变低，
    所以评分时用冻结那一刻的判定，不重算。"""
    why = list(rec.get("why") or [])
    first = why[0] if why else ""
    accept = bool(rec.get("accept"))
    m = re.search(r"threshold ([\d.]+)", first)
    plat = (rec.get("platform_source") or "_default") or "_default"
    pc = (filter_cfg.get("platforms") or {}).get(plat) or (filter_cfg.get("platforms") or {}).get("_default") or {}
    thr = float(m.group(1)) if m else float(pc.get("score_threshold", 60))
    if accept:
        layer = "override" if "type override" in first else "pass"
    elif first.startswith("score "):
        layer = "score"
    else:
        layer = "hard_gate"
    return Decision(accept=accept, would_accept=accept, score=float(rec.get("score") or 0), threshold=thr,
                    reasons=why, layer=layer, obs_id=int(rec.get("id") or 0), obs_type=normalize_type(rec.get("type")),
                    platform=plat, project=rec.get("project") or "")


def triage(obs: dict, filter_cfg: dict, cfg: dict, base: Decision | None = None) -> Verdict:
    """基础闸 → 规则加减 → 三档。只在 config 里 enabled=true 时，消费端才按 drop 跳过；本函数本身不看 enabled。
    base 给了就不重算闸（冻结样本用）。"""
    base = base or evaluate(obs, filter_cfg, None)
    hits = rule_hits(obs, cfg)
    adj = adjustment(hits, cfg, type_excess(obs, filter_cfg))
    thr = float(base.threshold or 0)
    score = round(float(base.score) + adj, 2)
    band = float(cfg.get("review_band", 15))
    protected = normalize_type(obs.get("type")) in set(cfg.get("protected_types") or ())
    v = Verdict(tier="keep", base_score=float(base.score), score=score, threshold=thr, hits=hits, adjust=adj, base=base)

    if not base.would_accept:
        # 只救分数差一点的（layer == score）；硬闸拒的不救
        if base.layer == "score" and adj > 0 and score >= thr and (thr - base.score) <= float(cfg.get("rescue_max_gap", 15)):
            v.tier, v.reason = "review", f"rescued: {base.score} + {adj} >= {thr}"
        else:
            v.tier, v.reason = "drop", f"base {base.layer}: {(base.reasons or [''])[0]}"
        return v

    if base.layer == "override" and (score >= thr or adj >= 0):
        v.reason = f"override: {base.obs_type}"
        return v

    if score < thr:
        v.tier = "review" if protected else "drop"
        v.reason = f"penalized: {base.score} {adj:+} < {thr}"
    elif score < thr + band:
        v.tier, v.reason = "review", f"within band: {score} < {thr} + {band}"
    else:
        v.reason = f"keep: {score} >= {thr} + {band}"
    return v


def triage_enabled(cfg: dict) -> bool:
    return bool(cfg.get("enabled", False))
