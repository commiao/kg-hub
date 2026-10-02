"""Paid model work overlaps; stale graph reads never pass the commit fence."""
from __future__ import annotations
import asyncio
import collections
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch, AsyncMock

from utils.graphiti_stage_adapter import StageArtifactStore
from utils.graphiti_parallel import (ReadDependencies, GraphReadConflict,
                                     _interval_wall_seconds, finish_optimistic_episode)


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
    async def test_graph_read_wall_counts_overlaps_once_and_includes_other_reads(self):
        self.assertEqual(_interval_wall_seconds([(1, 4), (2, 5), (8, 9)]), 5)
        with tempfile.TemporaryDirectory() as temp:
            reads = ReadDependencies(Driver(Graph()),
                                     StageArtifactStore(Path(temp) / 'stage.db'),
                                     ('source', 'sid', 'round', 'input'))
            await reads.read('MATCH candidates RETURN version', {}, phase='prepare')
            count, wall, other_wall = reads.read_summary('prepare')
            self.assertEqual(count, 1)
            self.assertGreater(wall, 0)
            self.assertEqual(other_wall, wall)
            self.assertEqual(reads.read_summary('validate'), (0, 0.0, 0.0))

    async def test_indexed_reads_serialize_across_observations_but_other_reads_overlap(self):
        class ConcurrentGraph:
            active = 0
            peak = 0
            async def ro_query(self, query, params):
                self.active += 1
                self.peak = max(self.peak, self.active)
                try:
                    await asyncio.sleep(.01)
                    return SimpleNamespace(header=[[1, 'uuid']], result_set=[[self.active]])
                finally:
                    self.active -= 1
        with tempfile.TemporaryDirectory() as temp:
            graph = ConcurrentGraph()
            store = StageArtifactStore(Path(temp) / 'stage.db')
            reads = [ReadDependencies(Driver(graph), store, ('s', str(i), 'o', 'd'))
                     for i in range(2)]
            indexed = 'CALL db.idx.vector.queryRelationships() WITH vec.cosineDistance(e.fact_embedding, $v) AS score RETURN e.uuid'
            with self.assertLogs('kg_hub.parallel', 'INFO'):
                results = await asyncio.gather(*(
                    reads[i % 2].read(indexed, {'v': [1.0]}, phase='prevalidate')
                    for i in range(8)))
            self.assertEqual(graph.peak, 1)
            self.assertTrue(all(result[0] == [{'uuid': 1}] for result in results))
            graph.peak = 0
            await asyncio.gather(*(reads[i % 2].read('MATCH (n) RETURN n.uuid', {})
                                   for i in range(8)))
            self.assertGreater(graph.peak, 1)

    async def test_similarity_timing_labels_prepare_and_validation_without_query_data(self):
        class TimedGraph(Graph):
            async def ro_query(self, query, params):
                return SimpleNamespace(header=[[1, 'uuid']], result_set=[])
        with tempfile.TemporaryDirectory() as temp:
            reads = ReadDependencies(Driver(TimedGraph()),
                                     StageArtifactStore(Path(temp) / 'stage.db'),
                                     ('source', 'sid', 'round', 'input'))
            edge = 'WITH vec.cosineDistance(e.fact_embedding, $search_vector) AS score WHERE e.uuid IN $edge_uuids'
            node = 'WITH vec.cosineDistance(n.name_embedding, $search_vector) AS score'
            with self.assertLogs('kg_hub.parallel', 'INFO') as logs:
                await reads.read(edge, {'search_vector': [0.1]}, phase='prepare')
                await reads.read(node, {'search_vector': [0.2]}, phase='prevalidate')
                await reads.read('MATCH (n) RETURN n', {}, phase='validate')
            self.assertEqual(len(logs.output), 2)
            self.assertIn('phase=prepare kind=edge scope=filtered seconds=', logs.output[0])
            self.assertIn('phase=prevalidate kind=node scope=group seconds=', logs.output[1])
            self.assertNotIn('search_vector', ''.join(logs.output))

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

    async def test_concurrent_validation_is_bounded_and_detects_any_change(self):
        class Versions(Graph):
            def __init__(self):
                super().__init__(); self.values={}; self.active=0; self.peak=0
            async def ro_query(self, query, params):
                self.active+=1; self.peak=max(self.peak,self.active)
                try:
                    await asyncio.sleep(.01)
                    value=self.values.get(params['query'],0)
                    return SimpleNamespace(header=[[1,'version']],result_set=[[value]])
                finally:
                    self.active-=1
        with tempfile.TemporaryDirectory() as temp:
            graph=Versions()
            reads=ReadDependencies(Driver(graph),StageArtifactStore(Path(temp)/'stage.db'),('s','i','o','d'))
            for i in range(20):
                await reads.execute('MATCH candidates RETURN version',query=str(i))
            graph.peak=0
            self.assertTrue(await reads.validate(4))
            self.assertEqual(graph.peak,4,'re-reads must overlap, bounded by the concurrency')
            graph.values['1']=1
            self.assertFalse(await reads.validate(4),'a change in any read is a conflict')
            self.assertEqual(graph.active,0,'no re-read may outlive validation')
            self.assertFalse(await reads.validate(1),'serial mode must agree')

    async def test_concurrent_validation_error_cancels_remaining_reads(self):
        class Failing(Graph):
            started=0
            async def ro_query(self, query, params):
                if params['query']=='boom' and self.value==1:
                    raise RuntimeError('falkor down')
                Failing.started+=1
                await asyncio.sleep(.2 if self.value==1 else 0)
                return SimpleNamespace(header=[[1,'version']],result_set=[[0]])
        with tempfile.TemporaryDirectory() as temp:
            graph=Failing(); graph.value=0
            reads=ReadDependencies(Driver(graph),StageArtifactStore(Path(temp)/'stage.db'),('s','i','o','d'))
            for q in ('a','boom','b','c'):
                await reads.execute('MATCH candidates RETURN version',query=q)
            graph.value=1
            with self.assertRaisesRegex(RuntimeError,'falkor down'):
                await asyncio.wait_for(reads.validate(4),1)

    async def test_slow_disk_batches_reads_without_blocking_network_loop(self):
        class SlowStore(StageArtifactStore):
            batches=0
            def save_batch_or_load(self,*args):
                time.sleep(.08)
                result=super().save_batch_or_load(*args)
                self.batches+=1
                return result
        with tempfile.TemporaryDirectory() as temp:
            store=SlowStore(Path(temp)/'stage.db')
            reads=ReadDependencies(Driver(Graph()),store,('s','i','o','d'))
            ticks=0;done=False
            async def heartbeat():
                nonlocal ticks
                while not done:
                    ticks+=1;await asyncio.sleep(.005)
            async def query(i):
                result=await reads.execute('MATCH candidates RETURN version', query=str(i))
                self.assertGreater(store.batches,0,'read must not return before durable commit')
                return result
            task=asyncio.create_task(heartbeat())
            try:
                await asyncio.gather(*(query(i) for i in range(12)))
            finally:
                done=True;await task
            self.assertEqual(store.batches,1,'concurrent reads share one fsync')
            self.assertGreaterEqual(ticks,5,'slow disk must not block event-loop I/O')
            restored=ReadDependencies(Driver(Graph()),store,('s','i','o','d'))
            self.assertEqual(len(restored.records),12)
            await restored.execute('MATCH candidates RETURN version',query='0')
            self.assertEqual(store.batches,1,'replaying unchanged reads must not fsync again')

    async def test_batch_failure_does_not_publish_partial_dependencies(self):
        with tempfile.TemporaryDirectory() as temp:
            store=StageArtifactStore(Path(temp)/'stage.db')
            with self.assertRaises(TypeError):
                store.save_batch_or_load('s','i','o','d',{'one':{'value':1},'two':object()})
            self.assertIsNone(store.save_or_load('s','i','o','d','one'))

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
    async def test_stale_round_skips_attribute_calls_and_replays_paid_receipt(self):
        from graphiti_core.nodes import EntityNode, EpisodicNode, EpisodeType
        import utils.graphiti_stage_adapter as stage
        with tempfile.TemporaryDirectory() as temp:
            store = StageArtifactStore(Path(temp) / 'stage.db')
            graph = Graph()
            driver = Driver(graph)
            graphiti = SimpleNamespace(driver=driver, clients=SimpleNamespace(driver=driver))
            calls = collections.Counter()

            async def resolve(clients, nodes, episode, previous, types, **kwargs):
                calls['resolve'] += 1
                await clients.driver.execute_query('MATCH candidates RETURN version')
                return nodes, {node.uuid: node.uuid for node in nodes}, []

            async def edges(*args, **kwargs):
                calls['edges'] += 1
                if calls['edges'] == 1:
                    graph.value = 1
                    store.save_or_load(kwargs['task_sd'], kwargs['task_sid'],
                                       kwargs['operation_id'], kwargs['input_digest'],
                                       'edge_phase', {'model_step_ids': ['old-paid']})
                return [], [], []

            async def attrs(g, nodes, *args, **kwargs):
                calls['attrs'] += 1
                return nodes

            @asynccontextmanager
            async def lock(**kwargs):
                yield

            async def commit(g, episode, nodes, edges, now, group, *args, **kwargs):
                calls['commit'] += 1
                return [], episode

            now = datetime.now(timezone.utc)
            node = EntityNode(name='test', group_id='kg_hub', name_embedding=[1.0])
            episode = EpisodicNode(name='test', group_id='kg_hub',
                                   source=EpisodeType.text, content='x',
                                   source_description='s', valid_at=now)
            with patch('utils.graphiti_parallel.async_writer_lock', lock), \
                 patch.object(stage, 'resolve_nodes_with_candidate_snapshot', resolve), \
                 patch.object(stage, 'extract_and_resolve_edges_with_snapshot', edges), \
                 patch.object(stage, 'extract_attributes_with_snapshot', attrs), \
                 patch.object(stage, 'commit_episode_with_receipt', commit), \
                 self.assertLogs('kg_hub.parallel', 'INFO') as logs:
                await finish_optimistic_episode(
                    graphiti, store=store, identity=('s', 'i', 'o', 'd'),
                    episode=episode, previous_episodes=[], extracted_nodes=[node],
                    node_episode_index_map={}, now=now, entity_types=None,
                    edge_type_map={}, group_id='kg_hub', edge_types=None,
                    custom_extraction_instructions=None)
            self.assertEqual(calls['attrs'], 1, 'stale round must not pay for attributes')
            self.assertEqual(calls['commit'], 1)
            conflict = store.save_or_load('s', 'i', 'o:graph-round:0', 'd', 'graph_conflict')
            self.assertEqual(conflict['model_step_ids'], ['old-paid'])
            self.assertTrue(conflict['early'])
            self.assertTrue(any('early=1 phase=before_attributes' in line
                                for line in logs.output))

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
                 patch.object(stage,'commit_episode_with_receipt',commit), \
                 self.assertLogs('kg_hub.parallel','INFO') as logs:
                results=await asyncio.wait_for(asyncio.gather(run('a'),run('b')),5)
            timing=[m for m in logs.output if '[ingest:parallel_timing]' in m]
            conflict=[m for m in logs.output if '[ingest:parallel_conflict]' in m]
            prepare=[m for m in logs.output if '[ingest:parallel_prepare]' in m]
            self.assertEqual((len(timing),len(conflict)),(2,1))
            self.assertEqual(len(prepare), 3)
            for field in ('nodes=', 'edges=', 'attributes=', 'embeddings=',
                          'other=', 'graph_reads=1', 'graph_read_wall=',
                          'graph_other_wall='):
                self.assertTrue(all(field in m for m in prepare), field)
            for field in ('lock_wait=','commit=','fence=','validate=','select=','begin=',
                          'write=','receipt_save=','loop_lag=','validate_concurrency='):
                self.assertTrue(all(field in m for m in timing),field)
            for field in ('lock_wait=','hold=','validate=','loop_lag='):
                self.assertIn(field,conflict[0])
            self.assertEqual(len(commits),2)
            self.assertEqual(len(set(commits)),2)
            self.assertEqual(len(model_rounds),3, 'one stale completed round must rebase')
            self.assertTrue(any(':graph-round:1' in x for x in model_rounds))
            for sid in ('a','b'):
                self.assertIsNotNone(store.save_or_load('source',sid,sid,'input','graph_commit_receipt'))

    async def test_completed_conflict_receipts_are_acknowledged_before_recovery(self):
        from graphiti_core.nodes import EpisodicNode, EpisodeType
        import utils.graphiti_stage_adapter as stage
        from model_gateway_client import _resume, model_operation
        import model_gateway_client as gateway
        with tempfile.TemporaryDirectory() as temp:
            store=StageArtifactStore(Path(temp)/'stage.db');driver=Driver(Graph())
            g=SimpleNamespace(driver=driver,clients=SimpleNamespace(driver=driver))
            old=('s','i','o:graph-round:0','d')
            store.save_or_load(*old,'graph_conflict',{'validated':False})
            store.save_or_load(*old,'prepared_commit',{'model_step_ids':['old-paid']})
            now=datetime.now(timezone.utc)
            episode=EpisodicNode(name='a',group_id='kg_hub',source=EpisodeType.text,
                                content='x',source_description='s',valid_at=now)
            async def resume(*args,**kwargs):
                self.assertEqual(_resume.get()['cached_pending'],set())
                self.assertEqual(kwargs['operation_id'],'o:graph-round:1')
                raise RuntimeError('stop before model')
            token=_resume.set({'cached_pending':{'old-paid'}})
            try:
                with patch.object(stage,'resolve_nodes_with_candidate_snapshot',resume):
                    with self.assertRaisesRegex(RuntimeError,'stop before model'):
                        await finish_optimistic_episode(g,store=store,identity=('s','i','o','d'),
                            episode=episode,previous_episodes=[],extracted_nodes=[],node_episode_index_map={},
                            now=now,entity_types=None,edge_type_map={},group_id='kg_hub',edge_types=None,
                            custom_extraction_instructions=None)
            finally:_resume.reset(token)
        with model_operation('ingest.episode','parent') as parent:
            with model_operation('ingest.graph-round','child') as child:
                child['fixed_envelope']=2
            self.assertEqual(parent,{'fixed_envelope':2})

    async def test_early_conflict_receipt_is_acknowledged_after_restart(self):
        from graphiti_core.nodes import EpisodicNode, EpisodeType
        import utils.graphiti_stage_adapter as stage
        from model_gateway_client import _resume
        with tempfile.TemporaryDirectory() as temp:
            store = StageArtifactStore(Path(temp) / 'stage.db')
            driver = Driver(Graph())
            graphiti = SimpleNamespace(driver=driver,
                                       clients=SimpleNamespace(driver=driver))
            store.save_or_load('s', 'i', 'o:graph-round:0', 'd', 'graph_conflict',
                               {'validated': False, 'early': True,
                                'model_step_ids': ['old-paid']})
            now = datetime.now(timezone.utc)
            episode = EpisodicNode(name='test', group_id='kg_hub',
                                   source=EpisodeType.text, content='x',
                                   source_description='s', valid_at=now)

            async def stop_at_next_round(*args, **kwargs):
                self.assertEqual(_resume.get()['cached_pending'], set())
                self.assertEqual(kwargs['operation_id'], 'o:graph-round:1')
                raise RuntimeError('stop before next model')

            token = _resume.set({'cached_pending': {'old-paid'}})
            try:
                with patch.object(stage, 'resolve_nodes_with_candidate_snapshot',
                                  stop_at_next_round):
                    with self.assertRaisesRegex(RuntimeError, 'stop before next model'):
                        await finish_optimistic_episode(
                            graphiti, store=store, identity=('s', 'i', 'o', 'd'),
                            episode=episode, previous_episodes=[], extracted_nodes=[],
                            node_episode_index_map={}, now=now, entity_types=None,
                            edge_type_map={}, group_id='kg_hub', edge_types=None,
                            custom_extraction_instructions=None)
            finally:
                _resume.reset(token)

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


class PrevalidatedCommitTests(unittest.IsolatedAsyncioTestCase):
    """Reads validated before the lock count only if no other holder came between."""
    def setUp(self):
        import utils.writer_lock as wl
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.dir=Path(temp.name)
        patcher=patch.multiple(wl,LOCK_DIR=self.dir/'locks',LOCK_FILE=self.dir/'locks'/'writer.lock')
        patcher.start();self.addCleanup(patcher.stop)

    async def _run(self,graph,*,after_prepare_read=None,during_prevalidate=None):
        from graphiti_core.nodes import EntityNode, EpisodicNode, EpisodeType
        import utils.graphiti_stage_adapter as stage
        store=StageArtifactStore(self.dir/'stage.db');driver=Driver(graph)
        graphiti=SimpleNamespace(driver=driver,clients=SimpleNamespace(driver=driver))
        rounds=[];commits=[]
        async def resolve(clients,nodes,*args,**kwargs):
            await clients.driver.execute_query('MATCH candidates RETURN version')
            rounds.append(kwargs['operation_id'])
            if after_prepare_read and len(rounds)==1: after_prepare_read()
            return nodes,{n.uuid:n.uuid for n in nodes},[]
        async def edges(*args,**kwargs): return [],[],[]
        async def attrs(g,nodes,*args,**kwargs): return nodes
        async def commit(g,episode,*args,**kwargs):
            commits.append(kwargs['operation_id']);graph.value+=1
            return [],episode
        original=graph.ro_query
        async def ro_query(query,params):
            result=await original(query,params)
            graph.reads+=1
            if during_prevalidate and graph.reads==3: during_prevalidate()
            return result
        graph.reads=0;graph.ro_query=ro_query
        now=datetime.now(timezone.utc)
        episode=EpisodicNode(name='a',group_id='kg_hub',source=EpisodeType.text,
                             content='x',source_description='s',valid_at=now)
        with patch.object(stage,'resolve_nodes_with_candidate_snapshot',resolve), \
             patch.object(stage,'extract_and_resolve_edges_with_snapshot',edges), \
             patch.object(stage,'extract_attributes_with_snapshot',attrs), \
             patch.object(stage,'commit_episode_with_receipt',commit), \
             self.assertLogs('kg_hub.parallel','INFO') as logs:
            await finish_optimistic_episode(
                graphiti,store=store,identity=('s','a','o','d'),episode=episode,
                previous_episodes=[],extracted_nodes=[EntityNode(name='n',group_id='kg_hub',name_embedding=[1.0])],
                node_episode_index_map={},now=now,entity_types=None,edge_type_map={},
                group_id='kg_hub',edge_types=None,custom_extraction_instructions=None)
        return rounds,commits,logs.output

    async def test_no_intervening_holder_skips_validation_under_the_lock(self):
        graph=Graph()
        rounds,commits,logs=await self._run(graph)
        self.assertEqual(len(commits),1)
        self.assertEqual(graph.reads,3,'one prepare read and two pre-lock re-reads only')
        self.assertIn('validate_skipped=1',[m for m in logs if 'parallel_timing' in m][0])

    async def test_holder_after_prevalidation_forces_validation_under_the_lock(self):
        import utils.writer_lock as wl
        graph=Graph()
        def other_writer():
            with wl.writer_lock(owner='other'):
                graph.value+=1
        rounds,commits,logs=await self._run(graph,during_prevalidate=other_writer)
        conflicts=[m for m in logs if 'parallel_conflict' in m]
        self.assertEqual(len(conflicts),1)
        self.assertNotIn('prevalidated=1',conflicts[0],'the change is only visible under the lock')
        self.assertEqual(rounds,['o:graph-round:0','o:graph-round:1'],
                         'the stale round must be rebuilt, never committed')
        self.assertEqual(commits,['o'])

    async def test_conflict_found_before_the_lock_never_takes_it(self):
        import utils.writer_lock as wl
        graph=Graph()
        def other_writer():
            graph.value+=1
        rounds,commits,logs=await self._run(graph,after_prepare_read=other_writer)
        conflicts=[m for m in logs if 'parallel_conflict' in m]
        self.assertEqual(len(conflicts),1)
        self.assertIn('early=1 phase=before_attributes',conflicts[0])
        self.assertEqual(rounds,['o:graph-round:0','o:graph-round:1'])
        self.assertEqual(commits,['o'])
        self.assertEqual(wl.read_generation(),2,'only the final commit took the lock')


if __name__=='__main__':unittest.main()
