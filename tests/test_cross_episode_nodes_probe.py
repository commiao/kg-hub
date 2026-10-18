"""Cross-observation isolation and identity gates for the non-writing pilot."""
import json
import unittest
from datetime import datetime, timezone

from graphiti_core.nodes import EpisodeType, EpisodicNode
from tools.cross_episode_nodes_probe import build_request, validate


class CrossEpisodeNodesProbeTests(unittest.TestCase):
    def sample(self, sid, current, previous):
        now = datetime(2026, 10, 2, tzinfo=timezone.utc)
        episode = EpisodicNode(name=sid, group_id="kg_hub", source=EpisodeType.text,
                               source_description=f"observation {sid}", content=current,
                               valid_at=now, created_at=now)
        prior = EpisodicNode(name=sid + "-prior", group_id="kg_hub", source=EpisodeType.text,
                             source_description=f"history {sid}", content=previous,
                             valid_at=now, created_at=now)
        return {"sid": sid, "episode": episode.model_dump(),
                "previous_episodes": [prior.model_dump()]}

    def test_original_text_prompts_keep_current_sources_separate(self):
        request = build_request([self.sample("first", "Alpha uses X.", "Only alpha history."),
                                 self.sample("second", "Beta uses Y.", "Only beta history.")])
        first = request["original_messages"][0][1][1]["content"]
        second = request["original_messages"][1][1][1]["content"]
        self.assertIn("Alpha uses X.", first)
        self.assertNotIn("Beta uses Y.", first)
        self.assertIn("Beta uses Y.", second)
        self.assertNotIn("Alpha uses X.", second)
        self.assertNotIn("Only alpha history.", first)
        self.assertNotIn("Only beta history.", second)
        wrapped = json.loads(request["messages"][1]["content"])
        self.assertEqual([item["source_obs_id"] for item in wrapped["items"]], ["first", "second"])
        self.assertEqual(wrapped["items"][0]["original_user_prompt"], first)
        self.assertEqual(wrapped["items"][1]["original_user_prompt"], second)

    def test_rejects_missing_duplicate_and_cross_source_attribution(self):
        request = build_request([self.sample("first", "Alpha uses X.", "A prior."),
                                 self.sample("second", "Beta uses Y.", "B prior.")])
        good = {"items": [
            {"source_obs_id": "first", "extracted_entities": [
                {"name": "Alpha", "entity_type_id": 0, "episode_indices": [0]}]},
            {"source_obs_id": "second", "extracted_entities": [
                {"name": "Beta", "entity_type_id": 0, "episode_indices": [0]}]},
        ]}
        self.assertEqual(set(validate(request, good)), {"first", "second"})
        for payload in (
            {"items": good["items"][:1]},
            {"items": [good["items"][0], good["items"][0]]},
            {"items": [good["items"][0], {**good["items"][1], "source_obs_id": "unknown"}]},
            {"items": [good["items"][0], {**good["items"][1], "extracted_entities": [
                {"name": "Beta", "entity_type_id": 0, "episode_indices": [1]}]}]},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                validate(request, payload)
