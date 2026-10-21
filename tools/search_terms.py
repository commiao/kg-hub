"""Safe literal fallback helpers for multi-term retrieval queries.

FalkorDB fulltext does not tokenize Chinese text.  A user query such as
``公众号 阅读量`` therefore needs an explicit all-terms fallback rather than a
single literal substring match.  These helpers only construct placeholders
from bounded, normalized terms; user text is always passed as query params.
"""

import math
import re
from typing import Iterable


_TERM_SPLIT = re.compile(r"[\s,，、;；|/]+")
_MAX_TERMS = 4
_MAX_TERM_LENGTH = 32


def bounded_terms(query: str) -> tuple[str, ...]:
    """Return a small, de-duplicated term set for literal all-terms fallback."""
    seen: set[str] = set()
    terms: list[str] = []
    for raw in _TERM_SPLIT.split(query.strip().lower()):
        term = raw.strip("'\"()[]{}")
        if len(term) < 2 or len(term) > _MAX_TERM_LENGTH or term in seen:
            continue
        seen.add(term)
        terms.append(term)
        if len(terms) == _MAX_TERMS:
            break
    return tuple(terms) if 2 <= len(terms) <= _MAX_TERMS else ()


# 查询里夹着的英文标识符/ASCII 词，如「launchctl bootout 之后马上 bootstrap」、
# 「model-attempts.sqlite3 全表扫描」。整句子串与 bounded_terms 都要求中文虚词也出现在
# 事实里，这类问法永远匹配不上，所以把 ASCII 片段单独拿出来走 RELATES_TO.fact 的全文索引。
# 一个片段 = 用 . - / : + 连起来的一串，比如 model-attempts.sqlite3；全文索引在这些符号处
# 断词，所以片段内的词要求同时出现（AND），片段之间由调用方按稀有度加权。
_IDENT_RUN = re.compile(r"[A-Za-z0-9_]+(?:[.\-/:+][A-Za-z0-9_]+)*")
_IDENT_TOKEN = re.compile(r"[A-Za-z0-9_]+")
# 与 graphiti FalkorDriver 的 STOPWORDS 相同；RELATES_TO 的全文索引本身不去停用词。
_IDENT_STOPWORDS = frozenset(
    "a an and are as at be but by for if in into is it no not of on or such that the "
    "their then there these they this to was will with".split()
)
_MAX_IDENT_UNITS = 12
_MAX_IDENT_TOKEN_LENGTH = 40


def ident_units(query: str) -> tuple[tuple[str, ...], ...]:
    """Return ASCII identifier units, each a tuple of lowercase fulltext tokens.

    Tokens are ``[a-z0-9_]`` only, so they can be placed into a RediSearch query
    without escaping.  Single letters, stopwords and one/two-digit numbers are
    dropped because they match a large share of facts.
    """
    seen: set[tuple[str, ...]] = set()
    units: list[tuple[str, ...]] = []
    for run in _IDENT_RUN.findall(query):
        tokens: list[str] = []
        for raw in _IDENT_TOKEN.findall(run.lower()):
            if (len(raw) < 2 or len(raw) > _MAX_IDENT_TOKEN_LENGTH
                    or raw in _IDENT_STOPWORDS or (raw.isdigit() and len(raw) < 3)):
                continue
            if raw not in tokens:
                tokens.append(raw)
        unit = tuple(tokens)
        if not unit or unit in seen:
            continue
        seen.add(unit)
        units.append(unit)
        if len(units) == _MAX_IDENT_UNITS:
            break
    return tuple(units)


def _ident_token_query(token: str) -> str:
    # 全文索引不在中文标点/汉字处断词，「XAUTOCLAIM；Stream」被存成一个词，精确词查不到。
    # 召回时用前缀兜住 ASCII 在前、紧贴中文的写法；前缀带进来的 InvalidCollector 之类
    # 由 ident_coverage 按完整词再判一次。短词和纯数字用前缀会扩出一大片，保持精确。
    return token if len(token) < 4 or token.isdigit() else token + "*"


def ident_fulltext_query(unit: tuple[str, ...]) -> str:
    """RediSearch query for one unit over ``RELATES_TO.fact`` (its tokens AND-ed)."""
    tokens = [_ident_token_query(token) for token in unit]
    return "@fact:(" + " ".join(tokens) + ")" if tokens else ""


# 片段命中的事实数（df）超过这个数就算泛词（如 error、nas、mac），只参与覆盖度分母、
# 不拿来召回候选。3000 ≈ 全图 RELATES_TO 的 1.7%；离线网格里 1000 与 3000 只差原黄金集 literal 1 条。
IDENT_DF_MAX = 3000
# 覆盖度 = 事实命中的片段 idf 之和 / 查询里所有在图中出现过的片段 idf 之和。
IDENT_MIN_COVERAGE = 0.5


def ident_weights(units: Iterable[tuple[str, ...]], dfs: dict, total: int) -> dict:
    """idf weight per unit that occurs in the graph; units with df 0 cannot help and are dropped."""
    total = max(int(total or 0), 1)
    return {unit: math.log(1 + total / dfs[unit]) for unit in units if dfs.get(unit, 0) > 0}


def _token_matches(query_token: str, fact_token: str) -> bool:
    # 完整词，只放宽英文复数。不用前缀：「Invalid Type」不该算中 InvalidCollector。
    if query_token.isdigit():
        return fact_token == query_token
    return fact_token in (query_token, query_token + "s", query_token + "es")


def ident_coverage(fact: str, weights: dict) -> float:
    """Share of the query's identifier weight that ``fact`` contains (0..1)."""
    total = sum(weights.values())
    if not fact or total <= 0:
        return 0.0
    # 按 ASCII 字符切，汉字和中文标点也算边界：「XAUTOCLAIM；Stream」在这里是完整的 xautoclaim。
    fact_tokens = set(_IDENT_TOKEN.findall(fact.lower()))
    hit = sum(weight for unit, weight in weights.items()
              if all(any(_token_matches(t, f) for f in fact_tokens) for t in unit))
    return hit / total


def all_terms_clause(terms: Iterable[str], fields: tuple[str, ...]) -> tuple[str, dict[str, str]]:
    """Build ``AND``-joined CONTAINS predicates for a trusted static field list."""
    clauses: list[str] = []
    params: dict[str, str] = {}
    for index, term in enumerate(terms):
        key = f"literal_term_{index}"
        fields_clause = " OR ".join(f"toLower({field}) CONTAINS ${key}" for field in fields)
        clauses.append(f"({fields_clause})")
        params[key] = term
    return " AND ".join(clauses), params
