# Codex CLI — kg-hub PUSH Integration

## Status

**Template ready, activation requires Codex plugin system access.**

Codex CLI loads hooks via its **plugin marketplace** mechanism (not a user-level
`hooks.json` like Claude Code). Inspecting `~/.codex/config.toml` shows hook
state is tracked per-plugin:

```toml
[hooks.state."claude-mem@claude-mem-local:hooks/codex-hooks.json:post_tool_use:0:0"]
```

So adding kg-hub's PUSH hook requires kg-hub to be registered as a Codex
plugin — same pattern as claude-mem's installation via
`/Users/mac/.claude/plugins/marketplaces/thedotmack`.

## What's prepared

`plugin/hooks/codex-hooks.json` in this repo contains the hook definition:

- **SessionStart** (matcher: `startup|resume`) → calls
  `kg_push_hook.py --format codex` with 10 s timeout
- statusMessage: `Loading kg-hub canonical context`

The Python script handles Codex stdin payload automatically (reads `cwd` field
or falls back to env vars) and outputs Codex-compatible JSON
(`{continue, context, additionalContext, message}`).

## To activate (requires Codex plugin schema knowledge)

### Option 1 — Register kg-hub as a local marketplace plugin

```bash
# In ~/.codex/config.toml, add (alongside existing claude-mem-local entry):

[marketplaces.kg-hub-local]
source_type = "local"
source = "/Users/mac/workspace_claudeCode/kg-hub"

[plugins."kg-hub-push@kg-hub-local"]
# (activation key — TBD based on Codex plugin docs)
```

Then create a plugin manifest file (path/format depends on Codex's plugin
schema — needs documentation that we haven't found yet).

### Option 2 — Modify claude-mem's codex-hooks.json directly

Risky (modifies a vendored plugin file that gets overwritten on update).
Only useful for short-term experiments. Would add this entry to claude-mem's
`hooks/codex-hooks.json` under `SessionStart.hooks`:

```json
{
  "type": "command",
  "command": "/Users/mac/workspace_claudeCode/kg-hub/spike-graphiti/.venv/bin/python /Users/mac/workspace_claudeCode/kg-hub/tools/kg_push_hook.py --format codex",
  "timeout": 10
}
```

### Option 3 — Wait for Codex plugin schema docs

If Codex publishes a plugin manifest spec, plug kg-hub in cleanly. Until then,
Codex users see kg-hub canonical content **indirectly** via claude-mem's
existing session-init hook (which surfaces claude-mem.db obs, some of which
mention canonical doc content).

## Workaround for now

Codex CLI **already supports kg-hub via MCP** (config.toml has
`[mcp_servers.kg]` registered). So Codex sessions CAN call `kg_search` and
`kg_episode_search` on demand — they just don't auto-inject at SessionStart.

Users running Codex who want kg-hub canonical content can either:
- Manually invoke an MCP query at session start ("先查 kg-hub 当前项目有什么 canonical")
- Or wait for the plugin activation path (above) to be implemented

## When/if you implement Option 1

Update this doc with the working steps, and remove this "Status" header.
