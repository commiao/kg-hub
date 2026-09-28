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

if __name__=='__main__':unittest.main()
