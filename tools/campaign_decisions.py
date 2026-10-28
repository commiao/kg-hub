"""Append-only, one-source decisions for newly claimed campaign batches.

No defaults, inferred verdicts or semantic certification. The ledger binds each
explicit review to frozen source/lookahead/result/rules and is checked again at
both quality-check and submission. Existing batches keep their original rules.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from tools import extract_campaign as ec, agent_extract as ax
from utils import process_receipts as receipts
from utils.extraction_quality import digest

VERSION = 'source-decisions-v1'
FLAGS = ('translation_complete', 'facts_complete', 'core_covered', 'ownership_checked')


def enable(cid, *, reason):
    if os.environ.get('KG_HUB_CAMPAIGN_SLOT') is not None or not reason.strip():
        raise ValueError('coordinator reason required')
    ec.tracking_guard()
    d = ec.campaign_dir(cid)
    with ec.locked(d):
        c = ec.load(d)
        if (c.get('dispatch') or {}).get('state') != 'draining':
            raise ValueError('enable source decisions while new claims are drained')
        c['source_decisions'] = {'version': VERSION, 'reason': reason, 'at': receipts.now()}
        ec.save(d, c)
    return c['source_decisions']


def record(cid, bid, decision, *, reviewer):
    d = ec.campaign_dir(cid)
    with ec.locked(d):
        c = ec.load(d); b = ec.find_batch(c, bid); ec.assert_worker_batch(c, b)
        if b.get('decision_protocol') != VERSION or b['status'] in ('submitted', 'released'):
            raise ValueError('one-source decisions require an opted-in open batch')
        # A list, a full sheet, or omitted verdict/flags cannot be defaulted.
        if (not isinstance(decision, dict) or type(decision.get('id')) is not int
                or decision['id'] not in b['ids'] or decision.get('verdict') not in ('ok', 'error')
                or any(type(decision.get(k)) is not bool for k in FLAGS)
                or not isinstance(decision.get('note'), str) or not decision['note'].strip()
                or not reviewer.strip()):
            raise ValueError('exactly one source, explicit verdict/four flags/note/reviewer required')
        batch, _ = ec._batch_rows(d, bid)
        results = ec._results(d, bid); prompt = ec.batch_rules_text(d, c, b)
        row = next((r for r in ax.quality.result_items(results) if r['id'] == decision['id']), None)
        if row is None:
            raise ValueError('decision source has no current candidate')
        context = ax.quality.context_digest(batch, prompt, row, ec.batch_identity(c, b))
        if decision.get('context_digest') != context:
            raise ValueError('decision source/lookahead/result/rules changed')
        value = {'version': VERSION, 'identity': ec.batch_identity(c, b), 'batch': bid,
                 'reviewer': reviewer, 'decision': decision}
        root = d / 'source-decisions' / bid / str(decision['id'])
        ident = digest(value)
        # Content-addressed revisions retain every old decision; repeated same
        # input is idempotent, never a new semantic approval.
        target = root / (ident + '.json')
        if not target.exists(): receipts.write(target, value)
        receipts.write(root / 'latest.json', {'path': str(target), 'sha256': digest(value)})
    return {'id': decision['id'], 'decision': str(target), 'sha256': ident,
            'semantic_truth_certified': False}


def errors(d, b, review):
    if b.get('decision_protocol') != VERSION:
        return []
    problems = []
    rows = review.get('items', []) if isinstance(review, dict) else []
    by_id = {r.get('id'): r for r in rows if isinstance(r, dict)}
    if len(rows) != len(by_id) or set(by_id) != set(b['ids']):
        return ['source_decisions: complete unique source coverage required']
    for oid in b['ids']:
        root = d / 'source-decisions' / b['id'] / str(oid)
        pointer = receipts.read(root / 'latest.json')
        path = Path(pointer.get('path', '/missing')).resolve()
        if not path.is_relative_to(root.resolve()) or path.name == 'latest.json':
            problems.append(f'obs {oid}: source_decision_missing'); continue
        value = receipts.read(path)
        if (pointer.get('sha256') != digest(value) or path.stem != digest(value)
                or value.get('version') != VERSION or value.get('batch') != b['id']
                or value.get('identity') != review.get('identity')
                or value.get('reviewer') != review.get('reviewer')
                or value.get('decision') != by_id[oid]):
            problems.append(f'obs {oid}: source_decision_changed_or_unregistered')
    return problems


def record_repair(cid,bid,request,*,reviewer):
    """Separate append-only repair receipt; never edits production decisions."""
    from utils.extraction_repair import parse_request
    request=parse_request(request)
    d=ec.campaign_dir(cid)
    with ec.locked(d):
        c=ec.load(d);b=ec.find_batch(c,bid);ec.assert_worker_batch(c,b)
        if b['status']!='submitted' or not reviewer.strip():
            raise ValueError('submitted own batch and explicit repair reviewer required')
        batch,_=ec._batch_rows(d,bid)
        matches=[i for i in batch['items'] if all(i[k]==request[k] for k in ('source_description','source_obs_id'))]
        payload=request.get('replacement')
        if len(matches)!=1 or not payload:
            raise ValueError('one original source and reviewed replacement required')
        item=matches[0];context=payload.get('quality_context',{});review=payload.get('quality_review',{})
        frozen={**batch,'items':[item]}
        if (context.get('batch')!=frozen or context.get('prompt_text')!=ec.batch_rules_text(d,c,b)
                or context.get('prompt_version')!=ec.episode_prompt_version(c,b['chain'])
                or review.get('identity')!=ec.batch_identity(c,b) or review.get('reviewer')!=reviewer
                or payload.get('extractor_session') not in (None,cid) or ax.quality.validate_payload_quality(payload)):
            raise ValueError('repair requires unchanged original source/lookahead/rules/identity and complete source review')
        value={'version':'source-repair-decision-v1','campaign':cid,'batch':bid,'source_id':item['id'],
               'owner_session':c.get('slot_sessions',{}).get(b['model'],{}).get(str(ec.batch_slot(c,b))),
               'request_sha256':digest(request),'request':request,'reviewer':reviewer,
               'production_batch_sha256':digest(batch),'rules_sha256':digest(ec.batch_rules_text(d,c,b))}
        path=d/'repair-decisions'/bid/(digest(value)+'.json')
        if not path.exists():receipts.write(path,value)
    return {'path':str(path),'sha256':digest(value),'request_sha256':value['request_sha256'],
            'production_decisions_unchanged':True,'semantic_truth_certified':False}
