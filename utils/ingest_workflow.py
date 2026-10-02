"""Persist the original ingest plan so a human retry can use the same worker."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import logging
import uuid

from model_gateway_client import manual_resume_stage, stable_operation_id
from utils.graphiti_stage_adapter import (
    StageArtifactStore, StageInputDrift, inspect_started_graph_commit,
)

_current = ContextVar("ingest_workflow", default=None)
log = logging.getLogger("kg_hub.ingest_workflow")


class WorkflowPlanDrift(RuntimeError):
    """A previous plan exists for this source, but this run has another identity."""


def current_workflow():
    return _current.get()


@contextmanager
def workflow_context(body, reference_time, epoch, route, journal_factory):
    with bind_workflow(open_workflow(body, reference_time, epoch, route,
                                     journal_factory)) as workflow:
        yield workflow


@contextmanager
def bind_workflow(workflow):
    token = _current.set(workflow)
    try:
        yield workflow
    finally:
        _current.reset(token)


def open_workflow(body, reference_time, epoch, route, journal_factory):
    """Durably record the task plan; blocking, so async callers use a thread."""
    try:
        journal = journal_factory()
        if journal is None:
            raise RuntimeError("ingest journal unavailable")
        store = StageArtifactStore(journal.path)
        digest = stable_operation_id(
            body.name, body.episode_body, body.source_description,
            body.source_obs_id, reference_time.isoformat(), epoch, route)
        plan = store.save_or_load(
            body.source_description, body.source_obs_id, "business-task", digest,
            "task_plan", {
                "name": body.name, "source_description": body.source_description,
                "source_obs_id": body.source_obs_id, "epoch": epoch, "route": route,
                "body_digest": hashlib.sha256(body.episode_body.encode()).hexdigest(),
                "reference_time": reference_time.isoformat(),
                "parent_uuid": str(uuid.uuid4()) if route in {"split", "catalog"} else None,
            })
        return {"store": store, "plan": plan, "digest": digest}
    except StageInputDrift as exc:
        # A refinery retry may have a new execution epoch. Resume the original
        # model and graph identities only when all business inputs still match.
        prior = store.locate(body.source_description, body.source_obs_id,
                             "business-task", "task_plan")
        old_digest, old_plan = prior if prior is not None else (None, None)
        if old_plan is not None and all((
            old_plan.get("name") == body.name,
            old_plan.get("source_description") == body.source_description,
            old_plan.get("source_obs_id") == body.source_obs_id,
            old_plan.get("body_digest") == hashlib.sha256(body.episode_body.encode()).hexdigest(),
            old_plan.get("reference_time") == reference_time.isoformat(),
            old_plan.get("route") == route,
        )):
            log.info("[ingest:workflow_plan_reused] original business plan restored")
            return {"store": store, "plan": old_plan, "digest": old_digest}
        # Changed content/context cannot use old paid receipts or an
        # uncheckpointed serial fallback.
        raise WorkflowPlanDrift("existing ingest plan has another identity") from exc
    except Exception:
        if manual_resume_stage() is not None:
            raise
        log.exception("[ingest:checkpoint_unavailable] original worker continues")
        return None


def split_observations(new_value=None, *, step_ids=()):
    workflow = current_workflow()
    if workflow is None:
        return new_value
    plan = workflow["plan"]
    saved = workflow["store"].save_or_load(
        plan["source_description"], plan["source_obs_id"], "business-task",
        workflow["digest"], "split_observations",
        {"observations": new_value, "model_step_ids": sorted(step_ids)}
        if new_value is not None else None)
    if saved is None:
        return None
    from model_gateway_client import acknowledge_restored_steps
    acknowledge_restored_steps(saved["model_step_ids"])
    return saved["observations"]


async def verify_task_plan(driver, journal, row, *, observation_body):
    """Return None for legacy tasks, otherwise verify every planned graph write.

    The supported ingest path has no saga/community writes. Its complete core
    graph transaction is the business result, including all planned children.
    """
    if journal is None:
        return None
    store = await asyncio.to_thread(StageArtifactStore, journal.path)
    sd, sid = row.get("source_description"), row.get("source_obs_id")
    located = await asyncio.to_thread(store.locate, sd, sid, "business-task", "task_plan")
    if located is None:
        return None
    digest, plan = located
    parent = plan.get("parent_uuid")
    operations = []
    if parent:
        parents, _, _ = await driver.execute_query(
            "MATCH (e:Episodic {uuid: $uuid}) "
            "RETURN e.name AS name, e.source_description AS source_description, e.content AS content",
            uuid=parent)
        if (len(parents) != 1 or parents[0].get("name") != plan["name"]
                or parents[0].get("source_description") != sd
                or hashlib.sha256(str(parents[0].get("content", "")).encode()).hexdigest()
                != plan["body_digest"]):
            return False
        row["episode_uuid"] = parent
    if plan["route"] == "split":
        observations = await asyncio.to_thread(
            store.save_or_load, sd, sid, "business-task", digest, "split_observations")
        if not observations:
            return False
        for index, obs in enumerate(observations["observations"], 1):
            name = f"{plan['name']}--obs-{index:02d}"
            operations.append(stable_operation_id(
                name, f"{sd} · predigest type={obs['type']}",
                observation_body(obs, plan["name"]), plan["epoch"]))
    elif plan["route"] == "episode":
        # The exact operation ID is recorded before its first Graphiti call.
        link = await asyncio.to_thread(
            store.save_or_load, sd, sid, "business-task", digest, "episode_operation")
        if not link:
            return False
        operations.append(link["operation_id"])
    elif plan["route"] != "catalog":
        return False
    for operation_id in operations:
        envelope = await asyncio.to_thread(
            store.locate, sd, sid, operation_id, "operation_envelope")
        if envelope is None:
            return False
        input_digest, value = envelope
        proof = await inspect_started_graph_commit(
            driver, store, task_sd=sd, task_sid=sid,
            operation_id=operation_id, input_digest=input_digest)
        if proof.get("phase") != "core_materialized" or proof.get("saga_expected"):
            return False
        if plan["route"] == "episode":
            row["episode_uuid"] = value["episode"]["uuid"]
    return True
