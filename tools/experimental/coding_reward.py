"""tools/coding_reward.py — RewardProvider for the `coding` scenario (SELF-EVOLVING §4).

reward(session, capsule) = outcome ∧ attribution, taken as the intersection:
  - outcome      : did this coding session succeed?  (hard signals first; abstain if none)
  - attribution  : was THIS capsule's content actually used? (Tier-1 term overlap)

  reward = 1.0   if outcome=success AND attribution=yes
         = 0.0   if outcome decided but (failed OR capsule unused)
         = None  if outcome undecidable → ABSTAIN (never learn from a maybe)

v1 outcome reads claude-mem observation text markers only (conservative: specific
build/test/CI/verify/merge words, not generic "完成/成功"). Git "not reverted" / PR
"merged" are stronger and reserved for a later pass. Honest about being a proxy.
"""

from __future__ import annotations

from collections import defaultdict

from tools.experimental.engagement_audit import extract_terms, fetch_capsule_texts

# Conservative, specific outcome markers (lowercased substring match).
_SUCCESS = ["测试通过", "tests passed", "test passed", "build passed", "build succeeded",
            "编译通过", "编译成功", "ci 通过", "ci passed", "验证通过", "验证成功",
            "通过验证", "verified", "跑通", "merged", "合并成功", "部署成功", "all pass"]
_FAIL = ["测试失败", "test failed", "tests failed", "build failed", "编译失败",
         "回滚", "rollback", "reverted", "broke ", "broken", "失败告终", "不通过"]

ATTR_MIN_SHARED = 2   # engaged if >=1 capsule-unique term OR >=N shared distinctive terms


class CodingReward:
    scenario = "coding"

    def __init__(self):
        self._ready = False
        self.cap_terms: dict[str, set] = {}
        self.unique_terms: dict[str, set] = {}

    def prepare(self) -> None:
        """Fetch capsule texts + build the distinctive-term index (once).
        Raises if the kg-hub server is unreachable (caller decides to abort)."""
        cap_text = fetch_capsule_texts()
        self.cap_terms = {n: extract_terms(t) for n, t in cap_text.items()}
        df: dict[str, int] = defaultdict(int)
        for terms in self.cap_terms.values():
            for t in terms:
                df[t] += 1
        self.unique_terms = {n: {t for t in terms if df[t] == 1}
                             for n, terms in self.cap_terms.items()}
        self._ready = True

    @staticmethod
    def outcome(text: str) -> str | None:
        """'success' | 'fail' | None(undecidable → abstain)."""
        s = sum(text.count(m) for m in _SUCCESS)
        f = sum(text.count(m) for m in _FAIL)
        if s == 0 and f == 0:
            return None
        if s > f:
            return "success"
        if f > s:
            return "fail"
        return None  # tie → can't tell

    def attribution(self, capsule: str, text: str) -> bool:
        uniq = sum(1 for t in self.unique_terms.get(capsule, ()) if t in text)
        if uniq >= 1:
            return True
        shared = sum(1 for t in self.cap_terms.get(capsule, ()) if t in text)
        return shared >= ATTR_MIN_SHARED

    def reward(self, session: dict, capsule: str) -> float | None:
        if not self._ready:
            raise RuntimeError("CodingReward.prepare() not called")
        if capsule not in self.cap_terms:
            return None
        oc = self.outcome(session["text"])
        if oc is None:
            return None  # abstain: can't tell if the session succeeded
        return 1.0 if (oc == "success" and self.attribution(capsule, session["text"])) else 0.0
