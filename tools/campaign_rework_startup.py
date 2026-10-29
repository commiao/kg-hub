"""One explicit retry of a rework that failed before Codex initialized.

Only the exact local app-server initialization failure with no stdout events
qualifies. This does not clear tool approvals, provider errors or graph writes.
"""
from pathlib import Path
import re

from tools.campaign_ownership_recovery import _ref, _time
from utils import process_receipts as receipts


def validate(c, root, model, slot, session, worker, native, batches, assignment):
    events = Path(worker.get('events', '/missing')).resolve(strict=True)
    job = events.parent
    if (worker.get('mode') != 'quality_rework' or worker.get('batches') != batches
            or job.parent != (root / 'quality-reworks').resolve() or events.name != 'events.jsonl'
            or Path(worker.get('stderr', '/missing')).resolve() != job / 'stderr.log'
            or native.get('state') != 'completed' or native.get('exit_code') != 1
            or not native.get('attempt') or not native.get('child_pid') or not native.get('ended_at')
            or events.read_bytes() != b''):
        raise ValueError('startup retry requires a terminal original rework with no native events')
    stderr = job / 'stderr.log'
    lines = stderr.read_text().splitlines()
    failure = 'Error: failed to initialize in-process app-server client: Operation not permitted (os error 1)'
    warnings = [
        r'WARNING: proceeding, even though we could not create PATH aliases: Operation not permitted \(os error 1\)',
        r'\S+  WARN codex_state::runtime::logs_maintenance: failed to (?:prune diagnostic logs|checkpoint diagnostic logs): error returned from database: \(code: 8\) attempt to write a readonly database',
    ]
    if (not lines or lines[-1] != failure or any(not any(re.fullmatch(p, line) for p in warnings)
                                               for line in lines[:-1])):
        raise ValueError('only the exact pre-session local initialization failure may retry')
    scope = receipts.read(job / 'scope.json')
    end = receipts.read(job / 'exit-status.json')
    previous_worker = receipts.read(job / 'previous-worker.json')
    previous_native = receipts.read(job / 'previous-native.json')
    expected = {'campaign': c['id'], 'model': model, 'slot': slot, 'session': session,
                'batches': batches, 'new_source_quota': 0}
    if (any(scope.get(k) != v for k, v in expected.items())
            or end.get('session') != session or end.get('exit_code') != 1
            or abs(_time(end['ended_at']) - _time(native['ended_at'])) > 2
            or previous_worker.get('session') != session
            or previous_native.get('state') != 'completed' or previous_native.get('exit_code') != 0
            or not previous_native.get('attempt') or previous_native['attempt'] == native['attempt']
            or _time(previous_native['ended_at']) >= _time(native['started_at'])
            or scope.get('assignment') != _ref(job / 'assignment.md')
            or Path(assignment).read_bytes() != (job / 'assignment.md').read_bytes()):
        raise ValueError('startup retry requires unchanged assignment and original successful owner anchor')
    # The caller revalidates this original owner's transcript using the normal
    # rework rules. Never invent a clean terminal for the failed startup itself.
    evidence = {name: _ref(job / name) for name in (
        'events.jsonl', 'stderr.log', 'scope.json', 'exit-status.json',
        'previous-worker.json', 'previous-native.json', 'assignment.md')}
    evidence['failed_native'] = _ref(root / 'native/latest.json')
    evidence['failed_worker'] = _ref(root / 'worker.json')
    return previous_worker, previous_native, evidence
