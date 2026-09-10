# Dashboard status correction, 2026-09-07

The pipeline dashboard now shows current refinery activity separately from the
monthly throughput warning. Scheduled pauses do not imply a stalled worker.
Missing/stale heartbeats and explicit errors remain visible. The existing hour
bucket is labelled "本小时（UTC）"; the live pending ID difference is labelled an
estimate, not an exact queue count.

The topology gateway node combines quota with a read-only GET /health/ready,
cached for 60 seconds across simultaneous requests. It sends no caller token,
prompt or paid probe. It displays readiness issues and historical provider
results separately. The overall topology state reflects the gateway node.
Unrelated business quota exhaustion does not claim kg-hub quota is exhausted.

## Evidence

- 24 test files passed using the repository's per-file runner (project Python).
- Final focused tests: 9 dashboard-status + 8 topology-quota cases passed.
- Browser acceptance: both pipeline rows show scheduled pause; historical
  backlog remains 7786, monthly completed 26, daily completed 25, with an amber
  low-throughput warning.
- Gateway readiness still returns 503 for
  `rollback_witness_preflight_unresolved`; the witness contains one kg-hub
  preflight dated 2026-09-07T03:32:58+00:00. Other readiness checks pass.
- The actual refinery environment uses 22:00–10:00 Asia/Shanghai. Older
  comments claiming 22:00–05:00 are not the deployed configuration.
- Follow-up (2026-09-10): this remains historical deployment evidence. The
  production environment explicitly remains 22:00–10:00; the user requested
  22:00–08:00, so a safe release should change only
  `KG_HUB_REFINERY_WINDOW_END` to `8`.

## Deployment

NAS source: `/volume1/docker/kg-hub-src`.
Image: `kg-hub-server:dashboard-health-20260907`.
Compose: existing docker-compose.yml and model-gateway-network.override.yml,
plus `deploy/dashboard-health.override.yml`, service `kg_hub_server` only.
Original source backup: `.dashboard-health-backup.8lrVGz`.
Original image retained as `kg-hub-server:dashboard-health-base-20260907`.
Gateway process, witness records, credentials and quota policy were not changed.

Readiness is an aggregate diagnostic; the paid entry points reject replays by
request identity, not every new request due to any unrelated preflight. The
event that originally left this record unresolved has not been proven.
