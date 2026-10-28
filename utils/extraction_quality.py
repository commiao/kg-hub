"""Versioned, source-bound human review receipts shared by every extraction client.

These checks prove evidence was supplied and the reviewed bytes have not changed.
They cannot decide semantic truth: the named reviewer must read every statement.
"""
from __future__ import annotations

import hashlib
import json
import re

QUALITY_VERSION = "extraction-quality-v1"
EMPTY_REASONS = {"no_stable_knowledge", "duplicate", "unreadable"}


def requires_quality(prompt_version: str | None) -> bool:
    match = re.match(r"^agent-v(\d+)", prompt_version or "")
    return bool(match and int(match.group(1)) >= 5)


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


IDENTITY_FIELDS = ("extractor_model", "extractor_tool", "run_id")


def extraction_identity(model: str | None, executor: str | None, run_id: str | None) -> dict:
    return dict(zip(IDENTITY_FIELDS, (model, executor, run_id)))


def valid_identity(identity) -> bool:
    return (isinstance(identity, dict) and set(identity) == set(IDENTITY_FIELDS)
            and all(isinstance(identity[k], str) and identity[k].strip() for k in IDENTITY_FIELDS))


def context_digest(batch: dict, prompt_text: str, result: dict, identity: dict | None = None) -> str:
    return digest({"schema": QUALITY_VERSION, "batch": batch, "prompt_text": prompt_text,
                   "result": result, "identity": identity})


def result_items(results: dict) -> list[dict]:
    return results.get("items", [results] if "entities" in results else [])


def body_evidence(item: dict) -> str:
    # The recording project is not ownership evidence (Sol first-round incident).
    return "\n".join(line for line in item.get("episode_body", "").splitlines()
                     if not line.startswith("Project:"))


def lookahead_evidence(batch: dict) -> str:
    return "\n".join(row.get("title", "") if isinstance(row, dict) else str(row)
                     for row in batch.get("lookahead", []))


def evidence_text(evidence: dict, item: dict, sources: dict[str, dict], future: str) -> str:
    """Resolve only frozen input; a cross-item quote must name its exact source."""
    source = evidence.get("source")
    if source == "body":
        return body_evidence(item)
    if source == "lookahead":
        return future
    source_id = evidence.get("source_id")
    if (source == "batch_item" and type(source_id) is int
            and str(source_id) in sources and source_id != item["id"]):
        return body_evidence(sources[str(source_id)])
    return ""


def review_sheet(batch: dict, results: dict, prompt_text: str, identity: dict | None = None) -> dict:
    """Unapproved template: no default true flags or automatically invented quotes."""
    out = {"schema": QUALITY_VERSION, "reviewer": "", "identity": identity, "items": []}
    for result in result_items(results):
        row = {"id": result["id"], "context_digest": context_digest(batch, prompt_text, result, identity),
               "verdict": "pending", "translation_complete": False, "facts_complete": False,
               "core_covered": False, "ownership_checked": False,
               "edges": [{"index": n, "source": "body", "quote": ""}
                         for n, _ in enumerate(result.get("edges", []))],
               "summaries": [{"name": e["name"], "quote": ""}
                             for e in result.get("entities", []) if e.get("summary")], "empty": None}
        if not result.get("entities"):
            row["empty"] = {"reason": "", "quote": "", "no_new_information": False,
                            "duplicate_of": None, "covered_quote": ""}
        out["items"].append(row)
    return out


def _quote_ok(quote, text: str) -> bool:
    return (isinstance(quote, str) and bool(quote.strip()) and quote in text
            and (len(quote.strip()) >= 8 or quote == text.strip()))


def _validate_review(batch: dict, results: dict, review: dict | None, prompt_text: str) -> list[str]:
    """Validate full-batch coverage and evidence, returning actionable failures."""
    if not isinstance(review, dict) or review.get("schema") != QUALITY_VERSION:
        return ["quality_review_required: generate quality-sheet and complete a human/model review"]
    errors = []
    identity = review.get("identity")
    if not valid_identity(identity):
        errors.append("quality_identity_required: model, tool and run must be fixed before review")
    if not isinstance(review.get("reviewer"), str) or not review["reviewer"].strip():
        errors.append("quality_reviewer_required")
    if not isinstance(prompt_text, str) or not prompt_text.strip():
        errors.append("quality_prompt_required")
    sources = batch.get("items", [])
    rows = result_items(results)
    receipts = review.get("items", [])
    if not all(isinstance(x, list) for x in (sources, rows, receipts)):
        return errors + ["quality_items_must_be_lists"]
    if not all(isinstance(x, dict) and "id" in x for x in sources + rows + receipts):
        return errors + ["quality_items_invalid"]
    if not all(type(x["id"]) is int for x in sources + rows + receipts):
        return errors + ["quality_ids_must_be_integers"]
    ids = [str(x["id"]) for x in sources]
    rids = [str(x["id"]) for x in rows]
    qids = [str(x["id"]) for x in receipts]
    if len(set(ids)) != len(ids) or len(set(rids)) != len(rids) or len(set(qids)) != len(qids):
        return errors + ["quality_duplicate_id"]
    if set(ids) != set(rids) or set(rids) != set(qids):
        return errors + ["quality_coverage_mismatch: source, result and review ids must match"]
    by_result = {str(x["id"]): x for x in rows}
    by_review = {str(x["id"]): x for x in receipts}
    by_source = {str(x["id"]): x for x in sources}
    positions = {oid: n for n, oid in enumerate(ids)}
    future = lookahead_evidence(batch)
    for item in sources:
        oid = str(item["id"])
        result, receipt = by_result[oid], by_review[oid]
        prefix = f"obs {oid}: "
        if receipt.get("context_digest") != context_digest(batch, prompt_text, result, identity):
            errors.append(prefix + "quality_context_changed: regenerate and re-review")
        if receipt.get("verdict") != "ok":
            errors.append(prefix + "quality_verdict_not_ok")
        for flag in ("translation_complete", "facts_complete", "core_covered", "ownership_checked"):
            if receipt.get(flag) is not True:
                errors.append(prefix + flag + " must be reviewed true")
        body = body_evidence(item)
        entities, edges = result.get("entities", []), result.get("edges", [])
        if not isinstance(entities, list) or not isinstance(edges, list):
            errors.append(prefix + "entities_and_edges_must_be_lists")
            continue
        if not entities:
            empty = receipt.get("empty") or {}
            if edges:
                errors.append(prefix + "empty_with_edges")
            if empty.get("reason") not in EMPTY_REASONS or empty.get("no_new_information") is not True:
                errors.append(prefix + "empty_reason_and_no_new_information_required")
            empty_source = not body.strip() and empty.get("reason") == "unreadable" and empty.get("quote") == ""
            if not empty_source and not _quote_ok(empty.get("quote"), body):
                errors.append(prefix + "empty_source_quote_required")
            # Concrete historical test outcomes are facts, not mere replay metadata.
            # This is intentionally narrow: IDs, durations and encrypted blobs are not
            # automatically treated as valuable knowledge just because they contain digits.
            test_numbers = re.findall(r"\b\d+\s+(?:passed|failed|failing files)\b|\b\d+\s+files?\s+failed\b", body, re.I)
            if test_numbers and empty.get("reason") != "duplicate":
                errors.append(prefix + "empty_has_concrete_test_results: extract or prove previous coverage")
            if empty.get("reason") == "duplicate":
                prior = str(empty.get("duplicate_of"))
                if prior not in positions or positions[prior] >= positions[oid]:
                    errors.append(prefix + "duplicate_must_reference_previous_item")
                else:
                    previous = by_result[prior]
                    if not previous.get("entities"):
                        errors.append(prefix + "duplicate_reference_must_be_nonempty")
                    facts = "\n".join(e.get("fact", "") for e in previous.get("edges", []))
                    if not _quote_ok(empty.get("covered_quote"), facts):
                        errors.append(prefix + "duplicate_covered_fact_quote_required")
                    numbers = re.findall(r"\d+", " ".join(test_numbers))
                    covered_numbers = set(re.findall(r"\d+", str(empty.get("covered_quote", ""))))
                    if any(n not in covered_numbers for n in numbers):
                        errors.append(prefix + "duplicate_test_numbers_not_covered")
            continue
        if receipt.get("empty") is not None:
            errors.append(prefix + "nonempty_result_has_empty_review")
        from schema import ENTITY_TYPES, EDGE_TYPES
        from utils.single_pass_extract import observation_title
        names = {e["name"] for e in entities}
        if len(names) != len(entities) or any(e.get("type") not in ENTITY_TYPES for e in entities):
            errors.append(prefix + "invalid_or_duplicate_entity")
        for edge in edges:
            if (edge.get("source") not in names or edge.get("target") not in names
                    or edge.get("name") not in EDGE_TYPES):
                errors.append(prefix + "invalid_or_dangling_edge")
        # Invoke the writer's real normalizer: approved facts must survive ingestion.
        from utils.extracted_ingest import ExtractedEntity, ExtractedEdge, normalize_extraction
        normalized = normalize_extraction([ExtractedEntity(**e) for e in entities],
                                          [ExtractedEdge(**e) for e in edges],
                                          set(ENTITY_TYPES), set(EDGE_TYPES))
        losses = ("dup_entity", "dup_edge", "self_edge", "dangling_edge", "unknown_type", "offschema_edge")
        if (len(normalized.entities) != len(entities) or len(normalized.edges) != len(edges)
                or any(normalized.stats[k] for k in losses)):
            errors.append(prefix + "normalization_structure_loss: fix self, duplicate or invalid links")
        if not normalized.edges:
            errors.append(prefix + "nonempty_result_has_no_retained_facts")
        title = observation_title(item.get("episode_body", ""))
        first_fact = edges[0].get("fact", "") if edges else ""
        correction = first_fact[len(title):] if first_fact.startswith(title) else ""
        corrected_title = bool(re.fullmatch(r"（后被推翻：[^\n]+）", correction))
        scoped_title = bool(re.fullmatch(r"（证据范围：[^\n]+）", correction))
        if len(edges) < 2 or (first_fact != title and not corrected_title and not scoped_title):
            errors.append(prefix + "verbatim_title_required")
        elif edges[1].get("fact", "").strip() in {title.strip(), first_fact.strip()}:
            # Proper names can contain CJK in English prose; a reviewer decides
            # language/completeness using translation_complete, not a CJK guess.
            errors.append(prefix + "title_translation_identical")
        if any(len(e.get("fact", "")) > 200 for e in edges[2:]):
            errors.append(prefix + "fact_too_long: rewrite complete sentences, never slice")
        evidence = receipt.get("edges", [])
        if (not isinstance(evidence, list) or len(evidence) != len(edges)
                or not all(isinstance(e, dict) and type(e.get("index")) is int for e in evidence)
                or {e.get("index") for e in evidence if isinstance(e, dict)} != set(range(len(edges)))):
            errors.append(prefix + "every_edge_requires_unique_evidence")
            continue
        evidence = {e["index"]: e for e in evidence}
        if scoped_title:
            # An evidence limit does not claim the original title was later
            # disproved. Keep its exact text and bind both qualifications to
            # a separately explicit, source-backed review decision.
            scope = receipt.get('title_scope')
            qualification = correction[len('（证据范围：'):-1]
            translation = scope.get('translated_qualification') if isinstance(scope, dict) else None
            if (not isinstance(scope, dict) or scope.get('kind') != 'evidence_limit'
                    or scope.get('qualification') != qualification
                    or not isinstance(scope.get('note'), str) or len(scope['note'].strip()) < 8
                    or not _quote_ok(scope.get('quote'), evidence_text(scope, item, by_source, future))
                    or not isinstance(translation, str) or len(translation.strip()) < 8
                    or translation not in edges[1].get('fact', '')):
                errors.append(prefix + 'scoped_title_requires_explicit_bilingual_evidence_limit')
            if receipt.get('title_correction') is not None:
                errors.append(prefix + 'evidence_limit_is_not_title_reversal')
        elif receipt.get('title_scope') is not None:
            errors.append(prefix + 'title_scope_requires_qualified_verbatim_title')
        if corrected_title:
            # Older receipts used the first edge's lookahead quote. Keep those
            # valid; body/same-batch corrections need their own explicit proof.
            correction_evidence = receipt.get("title_correction")
            if correction_evidence is None and evidence[0].get("source") == "lookahead":
                correction_evidence = evidence[0]
            if (not isinstance(correction_evidence, dict)
                    or not _quote_ok(correction_evidence.get("quote"),
                                     evidence_text(correction_evidence, item, by_source, future))):
                errors.append(prefix + "corrected_title_requires_correction_evidence")
        for n, edge in enumerate(edges):
            ev = evidence[n]
            source = ev.get("source")
            text = evidence_text(ev, item, by_source, future)
            if not _quote_ok(ev.get("quote"), text):
                errors.append(prefix + f"edge {n}: source_quote_required")
            if edge.get("name") == "belongs_to":
                # Ownership cannot be inferred from lookahead/recording metadata.
                target = edge.get("target", "")
                if source != "body" or not target or target.lower() not in str(ev.get("quote", "")).lower():
                    errors.append(prefix + f"edge {n}: ownership_body_evidence_required")
        summaries = receipt.get("summaries", [])
        if not isinstance(summaries, list):
            summaries = []
        required = {e["name"] for e in entities if e.get("summary")}
        names = [e.get("name") for e in summaries if isinstance(e, dict)]
        if len(names) != len(set(names)) or set(names) != required:
            errors.append(prefix + "every_summary_requires_unique_evidence")
        for ev in summaries:
            text = (evidence_text(ev, item, by_source, future) if isinstance(ev, dict) and "source" in ev
                    else body + "\n" + future)
            if not isinstance(ev, dict) or not _quote_ok(ev.get("quote"), text):
                errors.append(prefix + "summary_source_quote_required")
    return errors


def validate_review(batch: dict, results: dict, review: dict | None, prompt_text: str) -> list[str]:
    try:
        return _validate_review(batch, results, review, prompt_text)
    except (TypeError, ValueError, KeyError, AttributeError):
        return ["quality_review_malformed: objects, lists and strings required"]


def _validate_payload_quality(payload: dict) -> list[str]:
    """Server and clients use the exact same review contract for v5 requests."""
    if not requires_quality(payload.get("prompt_version")):
        return []
    context = payload.get("quality_context")
    if not isinstance(context, dict):
        return ["quality_context_required"]
    batch, results, prompt = context.get("batch"), context.get("results"), context.get("prompt_text")
    if not isinstance(batch, dict) or not isinstance(results, dict) or not isinstance(prompt, str):
        return ["quality_context_invalid"]
    if context.get("prompt_version") != payload.get("prompt_version"):
        return ["quality_prompt_version_mismatch"]
    review = payload.get("quality_review")
    if not isinstance(review, dict) or not valid_identity(review.get("identity")):
        return ["quality_identity_required"]
    for field in IDENTITY_FIELDS:
        if payload.get(field) != review["identity"][field]:
            return ["quality_identity_mismatch: " + field]
    frozen_version = batch.get("prompt_version")
    if frozen_version and frozen_version.split("+")[0] != payload["prompt_version"].split("+")[0]:
        return ["quality_frozen_prompt_version_mismatch"]
    current = next((x for x in batch.get("items", [])
                    if x.get("source_description") == payload.get("source_description")
                    and x.get("source_obs_id") == payload.get("source_obs_id")), None)
    if current is None or any(current.get(k) != payload.get(k)
                              for k in ("name", "episode_body", "reference_time")):
        return ["quality_source_mismatch"]
    result = next((x for x in result_items(results) if x.get("id") == current["id"]), None)
    if result is None:
        return ["quality_result_missing"]
    from_result = {"entities": [{"name": e["name"], "type": e["type"], "summary": e.get("summary", "")}
                                for e in result.get("entities", [])],
                   "edges": result.get("edges", [])}
    submitted = {"entities": payload.get("entities", []), "edges": payload.get("edges", [])}
    # Pydantic payload entities may contain default summary=""; normalized comparison is deliberate.
    submitted["entities"] = [{"name": e["name"], "type": e["type"], "summary": e.get("summary", "")}
                             for e in submitted["entities"]]
    if digest(from_result) != digest(submitted):
        return ["quality_result_mismatch"]
    if bool(payload.get("empty")) != (not bool(result.get("entities"))):
        return ["quality_empty_mismatch"]
    return validate_review(batch, results, payload.get("quality_review"), prompt)


def validate_payload_quality(payload: dict) -> list[str]:
    try:
        return _validate_payload_quality(payload)
    except (TypeError, ValueError, KeyError, AttributeError):
        return ["quality_payload_malformed: invalid source, context or result structure"]
