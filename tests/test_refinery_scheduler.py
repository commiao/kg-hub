"""A slow historical item must not block either queue or the next free slot."""
import asyncio
import unittest
from utils.refinery_scheduler import consume_fairly

class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_task_does_not_hold_next_slot_or_live_queue(self):
        release=asyncio.Event(); live=asyncio.Event(); later=asyncio.Event(); order=[]
        rows={'backlog':[{'id':i} for i in range(1,9)],'live':[{'id':100+i} for i in range(3)]}
        def refill(kind,attempted):return [r for r in rows[kind] if r['id'] not in attempted][:2]
        async def process(kind,row):
            order.append((kind,row['id']))
            if row['id']==1: await release.wait()
            if kind=='live':live.set()
            if row['id']==5:later.set()
        task=asyncio.create_task(consume_fairly(refill,process,can_submit=lambda:True,concurrency=2))
        await asyncio.wait_for(live.wait(),1)
        await asyncio.wait_for(later.wait(),1)
        self.assertFalse(task.done())
        release.set();self.assertEqual(await task,11)
        self.assertEqual(order[0],('backlog',1))
        self.assertEqual(len({i for _,i in order}),11)

    async def test_weighted_share_and_idle_capacity_are_used(self):
        seen=[]
        def refill(kind,attempted):
            base=0 if kind=='backlog' else 1000
            return [{'id':base+i} for i in range(1,101) if base+i not in attempted]
        async def process(kind,row):seen.append(kind)
        await consume_fairly(refill,process,can_submit=lambda:len(seen)<50,concurrency=1)
        self.assertEqual(seen.count('backlog'),40)
        self.assertEqual(seen.count('live'),10)
        seen.clear()
        await consume_fairly(lambda k,a:refill(k,a) if k=='live' else [],process,
                             can_submit=lambda:len(seen)<20,concurrency=1)
        self.assertEqual(seen,['live']*20)

    async def test_failure_drains_other_inflight_before_returning(self):
        other_started=asyncio.Event();other_finished=asyncio.Event();stop=False
        def refill(kind,attempted):return [{'id':i} for i in (1,2) if i not in attempted]
        async def process(kind,row):
            if row['id']==1:
                await other_started.wait()
                raise RuntimeError('failed')
            other_started.set();await asyncio.sleep(.02);other_finished.set()
        with self.assertRaisesRegex(RuntimeError,'failed'):
            await consume_fairly(refill,process,can_submit=lambda:True,concurrency=2)
        self.assertTrue(other_finished.is_set())

    async def test_pause_stops_refill_without_canceling_paid_work(self):
        paused=False;seen=[]
        def refill(kind,attempted):return [{'id':i} for i in range(10) if i not in attempted]
        async def process(kind,row):
            nonlocal paused
            seen.append(row['id']);paused=True
        await consume_fairly(refill,process,can_submit=lambda:not paused,concurrency=4)
        self.assertEqual(len(seen),1)

    async def test_cancel_waits_for_inflight_and_stops_new_work(self):
        started=asyncio.Event(); release=asyncio.Event(); finished=asyncio.Event(); seen=[]
        def refill(kind,attempted):return [{'id':i} for i in range(3) if i not in attempted]
        async def process(kind,row):
            seen.append(row['id']);started.set()
            await release.wait();finished.set()
        task=asyncio.create_task(consume_fairly(refill,process,can_submit=lambda:True,concurrency=1))
        await started.wait();task.cancel();await asyncio.sleep(0)
        self.assertFalse(task.done());release.set()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertTrue(finished.is_set());self.assertEqual(seen,[0])


def _failsafe(event, seconds=1.5):
    # A regression must fail an assertion, not hang the suite behind a shielded drain.
    asyncio.get_running_loop().call_later(seconds, event.set)


class RollingPoolTests(unittest.IsolatedAsyncioTestCase):
    """With a checkpoint the deadline is not a drain barrier (2026-09-28: ~21% idle)."""

    async def test_slots_keep_filling_past_the_deadline_while_a_slow_item_runs(self):
        release=asyncio.Event(); seen=[]; checkpoints=[]; _failsafe(release)
        def refill(kind,attempted):
            return [{'id':i} for i in range(1,30) if i not in attempted and i not in seen] if kind=='backlog' else []
        async def process(kind,row):
            seen.append(row['id'])
            if row['id']==1: await release.wait()
            else: await asyncio.sleep(.02)
            if len(seen)>=12: release.set()
        n=await consume_fairly(refill,process,can_submit=lambda:len(seen)<12 and not release.is_set(),concurrency=2,
                               active_seconds=.03,checkpoint=lambda:checkpoints.append(1))
        self.assertGreater(len(checkpoints),0)
        self.assertEqual(n,12); self.assertEqual(len(seen),12)

    async def test_without_checkpoint_the_deadline_still_stops_new_work(self):
        release=asyncio.Event(); seen=[]
        def refill(kind,attempted):return [{'id':i} for i in range(1,30) if i not in attempted]
        async def process(kind,row):
            seen.append(row['id'])
            if row['id']==1: await release.wait()
            else: await asyncio.sleep(.02)
        task=asyncio.create_task(consume_fairly(refill,process,can_submit=lambda:True,
                                                concurrency=2,active_seconds=.05))
        await asyncio.sleep(.2); taken=len(seen); release.set(); await task
        self.assertLess(taken,10); self.assertEqual(len(seen),taken)

    async def test_deferred_row_is_offered_again_after_a_checkpoint_but_inflight_is_not(self):
        release=asyncio.Event(); seen=[]; _failsafe(release)
        def refill(kind,attempted):
            return [{'id':i} for i in (1,2) if i not in attempted] if kind=='backlog' else []
        async def process(kind,row):
            seen.append(row['id'])
            if row['id']==1: await release.wait()
            elif seen.count(2)>=3: release.set()
        await consume_fairly(refill,process,can_submit=lambda:not release.is_set(),
                             concurrency=2,active_seconds=.02,checkpoint=lambda:None)
        self.assertEqual(seen.count(1),1, "在飞的不得被重复派发")
        self.assertGreaterEqual(seen.count(2),3, "被推迟的要在检查点后重试")

    async def test_idle_worker_waits_for_new_work_instead_of_exiting(self):
        release=asyncio.Event(); rows=[{'id':1}]; seen=[]; active=0; peak=0; _failsafe(release)
        def refill(kind,attempted):
            return [r for r in rows if r['id'] not in attempted and r['id'] not in seen] if kind=='backlog' else []
        def checkpoint():
            if len(rows)==1: rows.extend({'id':i} for i in (2,3))
        async def process(kind,row):
            nonlocal active,peak
            active+=1; peak=max(peak,active); seen.append(row['id'])
            if row['id']==1: await release.wait()
            else:
                await asyncio.sleep(.02)
                if {2,3}<=set(seen): release.set()
            active-=1
        await consume_fairly(refill,process,can_submit=lambda:True,concurrency=2,
                             active_seconds=.02,checkpoint=checkpoint)
        self.assertEqual(sorted(seen),[1,2,3])
        self.assertEqual(peak,2, "空闲槽位必须在检查点后接上新活")

    async def test_pause_ends_rolling_pool_after_inflight_drains(self):
        release=asyncio.Event(); paused=False; seen=[]
        def refill(kind,attempted):return [{'id':i} for i in range(10) if i not in attempted]
        async def process(kind,row):
            nonlocal paused
            seen.append(row['id'])
            if row['id']==0: await release.wait()
            else: paused=True; release.set()
        _failsafe(release)
        await consume_fairly(refill,process,can_submit=lambda:not paused,concurrency=2,
                             active_seconds=.02,checkpoint=lambda:None)
        self.assertEqual(sorted(seen),[0,1])

    async def test_failure_wakes_idle_workers_and_drains(self):
        release=asyncio.Event()
        def refill(kind,attempted):return [{'id':i} for i in (1,2) if i not in attempted] if kind=='backlog' else []
        async def process(kind,row):
            if row['id']==2:
                await asyncio.sleep(.01); raise RuntimeError('failed')
            await release.wait()
        task=asyncio.create_task(consume_fairly(refill,process,can_submit=lambda:True,concurrency=3,
                                                active_seconds=30,checkpoint=lambda:None))
        await asyncio.sleep(.05); self.assertFalse(task.done()); release.set()
        with self.assertRaisesRegex(RuntimeError,'failed'):
            await asyncio.wait_for(task,1)

    async def test_cancel_wakes_idle_workers_instead_of_waiting_out_the_period(self):
        # SIGTERM drain: idle workers must not sit out the checkpoint period.
        release=asyncio.Event(); started=asyncio.Event()
        def refill(kind,attempted):return [{'id':1}] if kind=='backlog' and 1 not in attempted else []
        async def process(kind,row):started.set();await release.wait()
        task=asyncio.create_task(consume_fairly(refill,process,can_submit=lambda:True,concurrency=2,
                                                active_seconds=1.0,checkpoint=lambda:None))
        await started.wait();await asyncio.sleep(.02)
        task.cancel();await asyncio.sleep(0);release.set()
        await asyncio.sleep(.3)
        self.assertTrue(task.done(), "取消后空闲 worker 必须立刻醒来退出")
        with self.assertRaises(asyncio.CancelledError):await task


if __name__=='__main__':unittest.main()
