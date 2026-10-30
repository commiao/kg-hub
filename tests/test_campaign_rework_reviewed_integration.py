"""Real receipt locks around evidence-only reconciliation; no live worker tests."""
import copy
import os
from pathlib import Path
import unittest
from unittest import mock

import test_campaign_rework_capacity as fixture
from tools import campaign_rework_capacity as capacity, campaign_supervision as supervision
from tools import campaign_rework as rework, extract_campaign as ec
from utils import process_receipts as receipts


class ReviewedIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.CapacityTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.addCleanup(self.f.doCleanups)
        self.job=self.f.fail_capacity()
        self.proof={'local_failures':[{'id':'host-pending','disposition':'host_failed_completion'}],
                    'evidence':{'original_events':capacity._ref(self.job/'events.jsonl')},
                    'reviewed_incident':{'new_source_quota':0,'normal_os_exit_proven':False}}

    def test_two_reconciliations_hold_actual_supervisor_and_native_locks(self):
        observed=[]
        def reconcile(*args):
            supervisor=receipts.status(self.f.worker);native=receipts.status(self.f.worker/'native')
            self.assertIs(supervisor['lease_held'],True)
            self.assertIs(native['lease_held'],True)
            self.assertEqual(supervisor['pid'],os.getpid())
            with self.assertRaises(receipts.Busy):
                with receipts.idle(self.f.worker/'native'):pass
            with self.assertRaises(receipts.Busy):
                with receipts.idle(self.f.worker):pass
            observed.append(True)
            return copy.deepcopy(self.proof)
        original=(self.job/'events.jsonl').read_bytes()
        with mock.patch.object(capacity,'reviewed_incident',side_effect=reconcile):
            result=self.f.retry()
        self.assertEqual(len(observed),2)
        self.assertEqual(len(self.f.spawned),1)
        self.assertEqual(result['session'],self.f.sid)
        self.assertEqual(result['new_source_quota'],0)
        self.assertEqual(ec.load(self.f.d)['dispatch']['state'],'draining')
        self.assertEqual((self.job/'events.jsonl').read_bytes(),original)
        saved=receipts.read(Path(result['evidence_dir'])/'capacity-retry.json')
        self.assertFalse(saved['reviewed_incident']['normal_os_exit_proven'])
        for ref in saved['evidence'].values():self.assertEqual(capacity._ref(ref['path']),ref)

    def test_reconciliation_rejection_cannot_start_native(self):
        with mock.patch.object(capacity,'reviewed_incident',side_effect=ValueError('unverified write')):
            with self.assertRaisesRegex(ValueError,'unverified write'):self.f.retry()
        self.assertFalse(self.f.spawned)

    def test_changed_second_readback_rejected_before_startup(self):
        changed=copy.deepcopy(self.proof);changed['reviewed_incident']['changed']='server request changed'
        with mock.patch.object(capacity,'reviewed_incident',side_effect=[self.proof,changed]):
            with self.assertRaisesRegex(ValueError,'changed during preflight'):self.f.retry()
        self.assertFalse(self.f.spawned)

    def test_backoff_budget_and_unknown_operation_precede_incident_reconciliation(self):
        with mock.patch.object(capacity,'reviewed_incident') as reconcile:
            with self.assertRaisesRegex(ValueError,'300-second'):self.f.run_rework(retry_capacity=True)
            reconcile.assert_not_called()
            op=self.f.d/'operations'/self.f.bid/'latest.json'
            value=receipts.read(op);value['may_have_written']=True;receipts.write(op,value)
            with self.assertRaisesRegex(ValueError,'known terminal'):self.f.retry()
            reconcile.assert_not_called()
            value['may_have_written']=False;receipts.write(op,value)
            worker=receipts.read(self.f.worker/'worker.json');worker['capacity_retries']=supervision.MAX_CAPACITY_RETRIES
            receipts.write(self.f.worker/'worker.json',worker)
            with self.assertRaisesRegex(ValueError,'budget exhausted'):self.f.retry()
            reconcile.assert_not_called()
        self.assertFalse(self.f.spawned)

    def test_failed_service_preflight_cannot_consume_valid_evidence(self):
        with mock.patch.object(capacity,'reviewed_incident',return_value=self.proof) as reconcile:
            with mock.patch.object(supervision,'CAPACITY_WAIT_SECONDS',0):
                with self.assertRaisesRegex(ValueError,'service preflight failed'):
                    rework.run(self.f.c['id'],'m',1,batches=[self.f.bid],assignment=self.f.assignment,
                        reason='review',retry_capacity=True,spawn=self.f.spawn,preflight=lambda *a:{'ok':False})
            self.assertEqual(reconcile.call_count,1)
        self.assertFalse(self.f.spawned)

    def test_original_busy_native_blocks_before_reconciliation(self):
        with receipts.idle(self.f.worker/'native'):
            with mock.patch.object(capacity,'reviewed_incident') as reconcile:
                with self.assertRaises(receipts.Busy):self.f.retry()
                reconcile.assert_not_called()
        self.assertFalse(self.f.spawned)


if __name__=='__main__':unittest.main()
