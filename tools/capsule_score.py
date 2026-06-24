"""tools/capsule_score.py — per-(capsule, scenario) self-evolving score store.

The self-evolving loop (docs/SELF-EVOLVING.md) keeps a score for each
(capsule, scenario) pair — NOT one global score per capsule — because a capsule's
usefulness is scenario-dependent (DESIGN helps in `planning`, ~useless in `coding`).

Score = a Beta posterior over P(useful | injected in this scenario):
  a = 1 + reward_sum ,  b = 1 + (n - reward_sum)   (Beta(1,1) uniform prior)
  mean = a / (a + b)
Thompson sampling later draws from Beta(a, b) for explore/exploit; for now we expose
mean() + the raw counts. Rewards arrive from per-scenario RewardProvider (step 2).

v1 storage: a local JSON side-table (zero NAS dependency, independently testable).
Can migrate to a FalkorDB node property later without changing this API.

API:
  s = ScoreStore()
  s.update("kg-hub-canonical-DESIGN", "coding", reward=0.0)   # reward in [0,1]; skip if None
  s.mean("kg-hub-canonical-DESIGN", "coding")                 # -> 0.5 if unseen (prior)
  s.report()                                                  # markdown table
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

STATE_PATH = Path.home() / ".kg-hub" / "state" / "capsule_scores.json"
_SEP = "\t"


class ScoreStore:
    def __init__(self, path: Path = STATE_PATH):
        self.path = path
        self.data: dict[str, dict] = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text())
            except Exception:
                self.data = {}

    @staticmethod
    def _key(capsule: str, scenario: str) -> str:
        return f"{capsule}{_SEP}{scenario}"

    def _entry(self, capsule: str, scenario: str) -> dict:
        return self.data.setdefault(
            self._key(capsule, scenario),
            {"n": 0, "reward_sum": 0.0, "exposures": 0, "updated": None},
        )

    def note_exposure(self, capsule: str, scenario: str) -> None:
        """Record that the capsule was injected into a session of this scenario.
        Exposure != reward; this just tracks routing volume per bucket."""
        e = self._entry(capsule, scenario)
        e["exposures"] += 1

    def update(self, capsule: str, scenario: str, reward: float | None) -> None:
        """Fold one reward in [0,1] into the bucket. reward=None → abstain (no-op)."""
        if reward is None:
            return
        reward = max(0.0, min(1.0, float(reward)))
        e = self._entry(capsule, scenario)
        e["n"] += 1
        e["reward_sum"] += reward
        e["updated"] = datetime.now(tz=timezone.utc).isoformat()

    def counts(self, capsule: str, scenario: str) -> tuple[float, float]:
        e = self.data.get(self._key(capsule, scenario))
        if not e:
            return 1.0, 1.0  # Beta(1,1) prior
        return 1.0 + e["reward_sum"], 1.0 + (e["n"] - e["reward_sum"])

    def mean(self, capsule: str, scenario: str) -> float:
        a, b = self.counts(capsule, scenario)
        return a / (a + b)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2))

    def report(self) -> str:
        L = ["| 胶囊 | 场景 | 曝光 | 评判数 n | 奖励均值 | 后验均值 |",
             "|---|---|---|---|---|---|"]
        for key, e in sorted(self.data.items(),
                             key=lambda kv: -(kv[1].get("exposures", 0))):
            cap, scen = key.split(_SEP)
            rmean = (e["reward_sum"] / e["n"]) if e["n"] else 0.0
            a, b = 1.0 + e["reward_sum"], 1.0 + (e["n"] - e["reward_sum"])
            L.append(f"| {cap.replace('kg-hub-canonical-','')} | {scen} | "
                     f"{e.get('exposures',0)} | {e['n']} | {rmean:.2f} | {a/(a+b):.2f} |")
        return "\n".join(L)


def _selftest() -> int:
    import tempfile
    p = Path(tempfile.mkdtemp()) / "scores.json"
    s = ScoreStore(p)
    assert s.mean("cap-A", "coding") == 0.5, "unseen → prior 0.5"
    for r in (1.0, 1.0, 0.0):           # 2 useful, 1 not
        s.update("cap-A", "coding", r)
    # a=1+2=3, b=1+(3-2)=2 → mean=3/5=0.6
    assert abs(s.mean("cap-A", "coding") - 0.6) < 1e-9, s.mean("cap-A", "coding")
    s.update("cap-A", "coding", None)   # abstain → no change
    assert abs(s.mean("cap-A", "coding") - 0.6) < 1e-9
    s.note_exposure("cap-A", "planning")
    assert s.mean("cap-A", "planning") == 0.5  # exposure ≠ reward
    s.save()
    assert p.exists()
    print("capsule_score selftest: OK")
    print(s.report())
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
