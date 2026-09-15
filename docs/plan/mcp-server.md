# Pueo MCP Server — Setup Guide

Pueo can expose its diagnostic tools to Home Assistant's built-in AI assistants (Claude,
OpenAI, etc.) via the MCP protocol. Once connected, you can ask your HA assistant questions
like "Why is my ZHA integration failing?" or "What is the current HA disk usage?" and it will
call Pueo's tools to answer.

## Prerequisites

- Pueo running on macOS (with the `mcp` Python package installed — added to `requirements.txt`)
- Home Assistant 2025.1 or later (the `mcp` client integration ships in that release)
- The Pueo host must be reachable from your HA host over the LAN

## Enable the MCP server

In your `config.yaml` (or `config/config.yaml` for Docker), add:

```yaml
mcp:
  enabled: true
  port: 8765          # must be reachable from HA over LAN
  token: ""           # optional; set a Bearer token if you want auth
```

Restart Pueo. You should see `mcp_server_starting port=8765` in the logs.

## Configure HA

In Home Assistant's `configuration.yaml`:

```yaml
mcp:
  servers:
    - name: "Pueo"
      url: "http://<pueo-hostname>:8765/sse"
      token: "<MCP_TOKEN>"    # omit if you left token blank
```

Replace `<pueo-hostname>` with the hostname or IP of the machine running Pueo (e.g.
`homeassistant-companion.local` or `192.168.1.10`).

Reload the HA configuration (`Developer Tools → YAML → Reload all`). The Pueo integration
should appear in Settings → Devices & Services → Model Context Protocol.

## What you can ask

| Example question | Tool Pueo uses |
|---|---|
| "What does Pueo know about recorder errors?" | `query_knowledge` |
| "What is the current HA disk usage?" | `get_disk_usage` |
| "Is the ZHA integration installed?" | `search_integrations` |
| "What is the state of sensor.temperature?" | `check_entity_status` |
| "Are there any entities missing from my dashboards?" | `get_dashboard_entity_health` |
| "What version of HA is running?" | `get_ha_profile` |
| "Show me recent HA log errors" | `read_logs` |

## Available tools (13)

| Tool | Description |
|---|---|
| `query_knowledge` | Search the Pueo knowledge base (runbooks, HA breaking changes, strategies) |
| `read_config` | Read HA configuration files |
| `read_logs` | Read recent HA log lines |
| `get_ha_profile` | HA and OS version, installed integrations |
| `get_disk_usage` | HA filesystem disk usage |
| `check_entity_status` | Current state of one or more entities |
| `read_pueo_log` | Pueo's own log |
| `search_log` | Search Pueo log by pattern |
| `fetch_ha_docs` | HA component source and documentation lookup |
| `remember` | Store a persistent note in Pueo's memory |
| `recall` | Retrieve notes from Pueo's memory |
| `search_integrations` | Search installed integrations and HACS add-ons by name |
| `get_dashboard_entity_health` | Lovelace dashboard entity health check |

## Security notes

- The MCP server binds to `0.0.0.0` (all interfaces) on `MCP_PORT`. It must be reachable
  from your HA host but should not be exposed to the internet.
- Set a `token` in `config.yaml` if your network has untrusted devices on the LAN. The token
  must also be added to HA's `configuration.yaml` as shown above.
- No write or repair tools are exposed via MCP — the worst an authorized caller can do is
  read config/logs and query the KB.

## Adding a token later

Update `config.yaml`:

```yaml
mcp:
  enabled: true
  port: 8765
  token: "my-secret-token"
```

And update HA's `configuration.yaml` to include `token: "my-secret-token"`. Restart Pueo.

## Troubleshooting

- **HA can't connect**: Check that `MCP_PORT` (default 8765) is not firewalled between HA
  and the Pueo host. Try `curl http://<pueo-host>:8765/sse` from the HA host.
- **401 Unauthorized**: Token mismatch. Verify `mcp.token` in Pueo's `config.yaml` matches
  the token in HA's `configuration.yaml`.
- **Pueo starts but no `mcp_server_starting` log**: Check `mcp.enabled: true` is set. If the
  `mcp` Python package is missing, Pueo logs `mcp_package_missing` and skips the server.
- **Tool returns an error**: The Pueo logs (Logs tab in the dashboard) will show
  `mcp_tool_dispatch_failed` with the tool name and error detail.
