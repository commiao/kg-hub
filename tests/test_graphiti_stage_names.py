import unittest

from utils.graphiti_stage_names import graphiti_model_stage


class GraphitiStageNameTests(unittest.TestCase):
    def test_pinned_model_prompts_map_to_replay_stage(self):
        expected = {
            "extract_nodes.extract_text": "node_extraction",
            "dedupe_nodes.nodes": "node_resolution",
            "extract_edges.edge": "edge_phase",
            "dedupe_edges.resolve_edge": "edge_phase",
            "extract_edges.extract_timestamps": "edge_phase",
            "extract_edges.extract_timestamps_batch": "edge_phase",
            "extract_nodes.extract_attributes": "attribute_phase",
            "extract_nodes.extract_summaries_batch": "attribute_phase",
        }
        for prompt, stage in expected.items():
            with self.subTest(prompt=prompt):
                self.assertEqual(graphiti_model_stage(prompt), stage)

    def test_unknown_prompt_is_not_guessed(self):
        self.assertIsNone(graphiti_model_stage("future.unreviewed_prompt"))
        self.assertIsNone(graphiti_model_stage(None))


if __name__ == "__main__":
    unittest.main()
