"""Find the exact original ingest input for a checkpointed task.

Historical backups can contain several submissions with the same business ID.
Recovery therefore requires a matching immutable Graphiti operation checkpoint;
it never guesses from timestamp, episode name, or the newest backup line.
"""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path

from utils.graphiti_episode_checkpoint import _input_digest


def recover_checkpointed_input(path: str | Path, journal, *,
                               source_description: str, source_obs_id: str,
                               episode_name: str, created_at: str,
                               stable_operation_id, group_id: str,
                               source_type) -> dict:
    matches: list[dict] = []
    with Path(path).open("r", encoding="utf-8") as file:
        for line in file:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if (item.get("source_description") != source_description
                    or item.get("source_obs_id") != source_obs_id
                    or item.get("name") != episode_name):
                continue
            body = item.get("episode_body")
            reference = item.get("reference_time")
            if not isinstance(body, str) or not isinstance(reference, str):
                continue
            try:
                ref_time = datetime.fromisoformat(reference.replace("Z", "+00:00"))
            except ValueError:
                continue
            operation_id = stable_operation_id(
                source_description, source_obs_id, episode_name, body, created_at)
            digest = _input_digest({
                "name": episode_name, "episode_body": body,
                "source_description": source_description,
                "reference_time": ref_time, "group_id": group_id,
            })
            try:
                previous = journal.read_episode_context(
                    source_description, source_obs_id, operation_id, digest)
            except RuntimeError as exc:
                if str(exc) != "episode input changed since model operation began":
                    raise
                continue
            if previous is not None:
                matches.append({"name": episode_name, "episode_body": body,
                                "source_description": source_description,
                                "source_obs_id": source_obs_id,
                                "reference_time": ref_time,
                                "source": source_type,
                                "operation_id": operation_id})
    if not matches:
        raise RuntimeError("no exact checkpointed ingest input")
    unique = {(row["episode_body"], row["reference_time"].isoformat())
              for row in matches}
    if len(unique) != 1:
        raise RuntimeError("multiple conflicting checkpointed inputs")
    return matches[0]
