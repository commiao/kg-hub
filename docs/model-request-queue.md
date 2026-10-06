# Shared model request queue

The production `create_gateway_client` sends named Anthropic request parameters
to the model gateway's durable `/v1/queue/submit` API and polls the same identity.
Business operation IDs, exact request parameters, structured-output repair and
stage checkpoints stay in kg-hub. Rate/quota/concurrency waiting is centralized
in the gateway, with SDK and client-side paid-request retries disabled.

Queue-owned model intents can resume after process interruption by submitting
exactly the same key/body. Older direct HTTP attempts retain their existing
reconciliation requirements. An unknown result does not authorize a new key.
The raw result remains in the gateway; the local model journal stores the
business-facing response. Human-authorized retries keep the original local
attempt limits and are submitted as new, explicitly granted queue identities.

After `business_result_persisted` verifies the graph result, a separate durable
business receipt is created. The application lifespan runs a receipt sender;
failed acknowledgements remain local and retry without a model invocation.
Model success and graph completion are therefore separate monitorable states.

Deploy the gateway queue API before this client. No fallback to a direct paid
model call occurs if the queue is unavailable. Existing graph ingest identifiers,
result checks and unresolved legacy attempts are preserved.

For the isolated Mac observer, the managed launcher reads the non-secret
`~/.claude-mem-next/gateway-queue.json` manifest. It accepts only the queue URL,
caller-token file path and business batch budgets. The token file must be
absolute and owner-only. Removing this manifest is not a migration rollback:
queue-owned work must keep its original executor and durable state.

The reviewed mac-office profile is `deploy/claude-mem/mac-office.queue.json`.
Install it atomically at the manifest path with mode 0600 after backing up the
previous file. It references the existing credvault-managed `.env`; no caller
secret is copied into Git or another credential file.

Merge only `deploy/claude-mem/mac-office.settings-overlay.json` into both
`~/.claude-mem/settings.json` and `~/.claude-mem-next/settings.json` with the
claude-mem `deploy/check_settings.py --approved ... --live ... --apply` tool.
Preserve the remaining settings and owner-only permissions. This disables hook
autostart so the managed isolated launcher owns process startup. Activate the
13.29 artifact and capture handoff journal before stopping the drained isolated
worker. Never stop the legacy RAM-only worker on port 37701.
