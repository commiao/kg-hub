"""Paid model work overlaps; stale graph reads never pass the commit fence."""
from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, AsyncMock

from utils.graphiti_stage_adapter import StageArtifactStore
from utils.graphiti_parallel import ReadDependencies, GraphReadConflict, finish_optimistic_episode


class Graph:
    def __init__(self):
        self.value = 0
    async def ro_query(self, query, params):
        if query != 'MATCH candidates RETURN version':
            raise RuntimeError('read-only rejects mutation')
        return SimpleNamespace(header=[[1, 'version']], result_set=[] if self.value is None else [[self.value]])


class Driver:
    _database = 'kg_hub'
    def __init__(self, graph):
        self.graph = graph
    def _get_graph(self, database):
        return self.graph


class ReadSetTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_or_changed_results_are_validated_after_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            store = StageArtifactStore(Path(temp)/'stage.db')
            graph = Graph(); graph.value = None; driver = Driver(graph)
            identity = ('source', 'sid', 'round', 'input')
            reads = ReadDependencies(driver, store, identity)
            await reads.execute('MATCH candidates RETURN version', query='fuzzy terms')
            restored = ReadDependencies(driver, StageArtifactStore(store.path), identity)
            self.assertTrue(await restored.validate())
            graph.value = 1
            self.assertFalse(await restored.validate())
            with self.assertRaises(GraphReadConflict):
                await restored.execute('MATCH candidates RETURN version', query='fuzzy terms')

    async def test_readonly_enforced_and_original_driver_never_mutated(self):
        with tempfile.TemporaryDirectory() as temp:
            store = StageArtifactStore(Path(temp)/'stage.db')
            driver = Driver(Graph())
            graphiti = SimpleNamespace(driver=driver, clients=SimpleNamespace(driver=driver))
            reads = ReadDependencies(driver, store, ('s','i','o','d'))
            view = reads.graphiti_view(graphiti)
            with self.assertRaisesRegex(RuntimeError, 'read-only'):
                await view.driver.execute_query('CREATE (n)')
            with self.assertRaisesRegex(RuntimeError, 'untracked'):
                view.driver.session()
            self.assertIs(graphiti.driver, driver)
            self.assertIs(graphiti.clients.driver, driver)
            self.assertIsNot(view.driver, driver)
            self.assertEqual(reads.records, {})


class ParallelCommitTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_overlapping_models_rebase_conflict_and_keep_original_receipts(self):
        from graphiti_core.nodes import EntityNode, EpisodicNode, EpisodeType
        from graphiti_core.graphiti import AddEpisodeResults
        import utils.graphiti_stage_adapter as stage
        with tempfile.TemporaryDirectory() as temp:
            store = StageArtifactStore(Path(temp)/'stage.db')
            graph = Graph(); driver = Driver(graph)
            graphiti = SimpleNamespace(driver=driver, clients=SimpleNamespace(driver=driver))
            gate = asyncio.Lock(); held = False; prepared = 0; both = asyncio.Event()
            commits=[]; model_rounds=[]
            @asynccontextmanager
            async def lock(**kwargs):
                nonlocal held
                async with gate:
                    held=True
                    try: yield
                    finally: held=False
            async def resolve(clients, nodes, episode, previous, types, **kwargs):
                nonlocal prepared
                self.assertFalse(held, 'model cannot wait under writer lock')
                await clients.driver.execute_query('MATCH candidates RETURN version')
                model_rounds.append(kwargs['operation_id'])
                prepared+=1
                if prepared>=2: both.set()
                await asyncio.wait_for(both.wait(), 2)
                return nodes,{n.uuid:n.uuid for n in nodes},[]
            async def edges(*args,**kwargs): return [],[],[]
            async def attrs(g,nodes,*args,**kwargs): return nodes
            async def commit(g,episode,nodes,edges,now,group,*args,**kwargs):
                self.assertTrue(held)
                i=(kwargs['task_sd'],kwargs['task_sid'],kwargs['operation_id'],kwargs['input_digest'])
                if not store.save_or_load(*i,'graph_commit_receipt'):
                    graph.value+=1
                    commits.append(episode.uuid)
                    store.save_or_load(*i,'graph_commit_receipt',{'done':True})
                return [],episode
            now=datetime.now(timezone.utc)
            async def run(sid):
                node=EntityNode(name=sid, group_id='kg_hub', name_embedding=[1.0])
                episode=EpisodicNode(name=sid,group_id='kg_hub',source=EpisodeType.text,
                                    content='x',source_description='source',valid_at=now)
                return await finish_optimistic_episode(
                    graphiti,store=store,identity=('source',sid,sid,'input'),episode=episode,
                    previous_episodes=[],extracted_nodes=[node],node_episode_index_map={},now=now,
                    entity_types=None,edge_type_map={},group_id='kg_hub',edge_types=None,
                    custom_extraction_instructions=None)
            with patch('utils.graphiti_parallel.async_writer_lock',lock), \
                 patch.object(stage,'resolve_nodes_with_candidate_snapshot',resolve), \
                 patch.object(stage,'extract_and_resolve_edges_with_snapshot',edges), \
                 patch.object(stage,'extract_attributes_with_snapshot',attrs), \
                 patch.object(stage,'commit_episode_with_receipt',commit):
                results=await asyncio.wait_for(asyncio.gather(run('a'),run('b')),5)
            self.assertEqual(len(commits),2)
            self.assertEqual(len(set(commits)),2)
            self.assertEqual(len(model_rounds),3, 'one stale completed round must rebase')
            self.assertTrue(any(':graph-round:1' in x for x in model_rounds))
            for sid in ('a','b'):
                self.assertIsNotNone(store.save_or_load('source',sid,sid,'input','graph_commit_receipt'))

    async def test_failed_model_stage_never_auto_retries(self):
        from graphiti_core.nodes import EntityNode, EpisodicNode, EpisodeType
        import utils.graphiti_stage_adapter as stage
        with tempfile.TemporaryDirectory() as temp:
            store=StageArtifactStore(Path(temp)/'stage.db');driver=Driver(Graph())
            g=SimpleNamespace(driver=driver,clients=SimpleNamespace(driver=driver))
            now=datetime.now(timezone.utc)
            episode=EpisodicNode(name='a',group_id='kg_hub',source=EpisodeType.text,
                                content='x',source_description='source',valid_at=now)
            calls=[]
            async def unknown(*args,**kwargs):
                calls.append(1)
                raise RuntimeError('paid outcome unknown')
            with patch.object(stage,'resolve_nodes_with_candidate_snapshot',unknown):
                with self.assertRaisesRegex(RuntimeError,'paid outcome unknown'):
                    await finish_optimistic_episode(g,store=store,identity=('s','i','o','d'),
                        episode=episode,previous_episodes=[],extracted_nodes=[],node_episode_index_map={},
                        now=now,entity_types=None,edge_type_map={},group_id='kg_hub',edge_types=None,
                        custom_extraction_instructions=None)
            self.assertEqual(len(calls),1)
            self.assertIsNone(store.save_or_load('s','i','o','d','graph_commit_started'))


class ExecutionModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_envelope_prevents_legacy_replay_after_rollback(self):
        import utils.graphiti_stage_adapter as stage
        from graphiti_core.nodes import EpisodeType
        from graphiti_core.search.search_utils import RELEVANT_SCHEMA_LIMIT
        with tempfile.TemporaryDirectory() as temp:
            store=StageArtifactStore(Path(temp)/'stage.db')
            driver=Driver(Graph())
            g=SimpleNamespace(driver=driver,clients=SimpleNamespace(driver=driver),
                              retrieve_episodes=AsyncMock(return_value=[]))
            kwargs=dict(store=store,task_sd='s',task_sid='i',operation_id='o',
                relevant_schema_limit=RELEVANT_SCHEMA_LIMIT,name='test',episode_body='body',
                source_description='s',reference_time=datetime.now(timezone.utc),
                group_id='kg_hub',source=EpisodeType.text)
            with patch.object(stage,'extract_nodes_with_snapshot',AsyncMock(return_value=([],{}))), \
                 patch('utils.graphiti_parallel.finish_optimistic_episode',AsyncMock(return_value='ok')):
                self.assertEqual(await stage.add_episode_with_stage_checkpoint(g,parallel=True,**kwargs),'ok')
            envelope=store.locate('s','i','o','operation_envelope')[1]
            self.assertEqual(envelope['schema_version'],'graphiti-core-0.29.0-parallel-v1')
            with self.assertRaisesRegex(RuntimeError,'legacy executor'):
                await stage.add_episode_with_stage_checkpoint(g,parallel=False,**kwargs)
            # The previous release has the same legacy schema check, even though
            # it knows nothing about execution_mode or parallel round artifacts.
            with self.assertRaisesRegex(RuntimeError,'envelope identity drift'):
                await stage._run_stage_checkpoint(g,optimistic=False,**kwargs)

    async def test_existing_paid_operation_keeps_legacy_lock(self):
        import utils.graphiti_stage_adapter as stage
        with tempfile.TemporaryDirectory() as temp:
            store=StageArtifactStore(Path(temp)/'stage.db')
            store.save_or_load('s','i','o','d','operation_envelope',{'existing':True})
            held=False
            @asynccontextmanager
            async def lock(**kwargs):
                nonlocal held
                held=True
                try: yield
                finally: held=False
            async def run(*args,**kwargs):
                self.assertTrue(held)
                self.assertFalse(kwargs.get('optimistic',False))
                return 'legacy'
            with patch('utils.writer_lock.async_writer_lock',lock),patch.object(stage,'_run_stage_checkpoint',run):
                result=await stage.add_episode_with_stage_checkpoint(None,store=store,
                    task_sd='s',task_sid='i',operation_id='o',relevant_schema_limit=5,parallel=True)
            self.assertEqual(result,'legacy')


if __name__=='__main__':unittest.main()
