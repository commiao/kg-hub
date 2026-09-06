"""Auditable, source-bound query aliases for known vocabulary gaps.

Aliases never write to the graph and never replace a direct result. Each rule
requires all listed fragments in a normalized user query and points at the
existing episode that justifies the vocabulary bridge.
"""

import json
import re
from pathlib import Path
from typing import NamedTuple


_CONFIG_PATH = Path(__file__).resolve().parents[1] / "docs" / "retrieval-aliases.json"
_WHITESPACE = re.compile(r"\s+")
_MAX_RULES = 32
_MAX_TERMS = 4
_MAX_TERM_LENGTH = 32


class RetrievalAlias(NamedTuple):
    """A validated alias selected for a particular query."""

    rule_id: str
    expand_terms: tuple[str, ...]
    source_episode: str


def normalize_query(value: str) -> str:
    return _WHITESPACE.sub(" ", value.strip().lower())


def _valid_terms(values: object, *, minimum: int = 1) -> tuple[str, ...]:
    if not isinstance(values, list) or not minimum <= len(values) <= _MAX_TERMS:
        return ()
    terms: list[str] = []
    for value in values:
        if not isinstance(value, str):
            return ()
        term = normalize_query(value)
        if not term or len(term) > _MAX_TERM_LENGTH or term in terms:
            return ()
        terms.append(term)
    return tuple(terms)


def _load_rules(config_path: Path = _CONFIG_PATH) -> tuple[dict[str, object], ...]:
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ()
    rules = data.get("rules") if isinstance(data, dict) else None
    if not isinstance(rules, list):
        return ()
    return tuple(rule for rule in rules[:_MAX_RULES] if isinstance(rule, dict))


def query_aliases(query: str, config_path: Path = _CONFIG_PATH) -> tuple[RetrievalAlias, ...]:
    """Return matching validated aliases in configuration order.

    A bad configuration entry is ignored rather than widening the search.
    """
    normalized = normalize_query(query)
    if not normalized:
        return ()
    matched: list[RetrievalAlias] = []
    for rule in _load_rules(config_path):
        rule_id = rule.get("id")
        source_episode = rule.get("source_episode")
        required = _valid_terms(rule.get("match_all"))
        expansion = _valid_terms(rule.get("expand_terms"))
        if (
            not isinstance(rule_id, str)
            or not rule_id
            or len(rule_id) > 80
            or not isinstance(source_episode, str)
            or not source_episode
            or not required
            or not expansion
            or not all(term in normalized for term in required)
        ):
            continue
        matched.append(RetrievalAlias(rule_id, expansion, source_episode))
    return tuple(matched)
