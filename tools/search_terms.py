"""Safe literal fallback helpers for multi-term retrieval queries.

FalkorDB fulltext does not tokenize Chinese text.  A user query such as
``公众号 阅读量`` therefore needs an explicit all-terms fallback rather than a
single literal substring match.  These helpers only construct placeholders
from bounded, normalized terms; user text is always passed as query params.
"""

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
