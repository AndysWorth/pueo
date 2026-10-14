# HA Supervisor CLI Reference

Trigger: run_ha_command, supervisor CLI, ha apps, ha core, ha os, ha backups, ha supervisor,
ha network, which CLI command, how to restart HA, how to update HA, how to list backups

## Overview

The `run_ha_command` tool runs `ha` CLI commands on the HA host via SSH, but **only the
commands listed under "Commands run_ha_command accepts"**. Anything else (including shell
chaining with `&&`, pipes, or `grep`) is rejected with "Command not in allowlist". The
other sections are a CLI reference so you can explain things to the user — do not call
them through `run_ha_command`.

## Commands run_ha_command accepts

Read-only (run directly):

```
run_ha_command("ha core check")                  # Validate config (exit 0 = valid)
run_ha_command("ha core info")                   # Core version, version_latest, state, boot
run_ha_command("ha os info")                     # OS version, version_latest, board, boot slot
run_ha_command("ha host info")                   # Hostname, kernel, disk, uptime
run_ha_command("ha resolution info --raw-json")  # Supervisor issues, suggestions, unhealthy
run_ha_command("ha backups list")                # Backups with slug, type (full/partial), date
run_ha_command("ha apps list")                   # Installed apps with slug, state, version
run_ha_command("ha apps info core_mosquitto")    # One app's details (any valid slug)
run_ha_command("ha core stats")                  # CPU / memory for Core (also --raw-json)
run_ha_command("ha supervisor stats")            # CPU / memory for Supervisor
run_ha_command("ha apps stats core_mosquitto")   # CPU / memory for one app
```

`ha dns|audio|multicast|observer|cli stats` (each optionally `--raw-json`) are also
accepted. For Supervisor version details use `ha os info` and `ha core info` —
`ha supervisor info` is **not** accepted.

State-changing (gated — queues an approval card below FULL_AUTONOMOUS, level 4):

```
run_ha_command("ha core restart")                          # MEDIUM — brief outage
run_ha_command("ha core stop")                             # HIGH
run_ha_command("ha supervisor repair")                     # MEDIUM — prune unused image layers
run_ha_command("ha apps restart core_mosquitto")           # MEDIUM — any valid slug
run_ha_command("ha resolution healthcheck")                # LOW — re-run Supervisor checks
```

`ha resolution suggestion apply <uuid>` (MEDIUM) and `ha resolution issue dismiss <uuid>`
(LOW) are accepted with a real UUID from `ha resolution info --raw-json`.

Logs: use `read_logs(log_source="ha_core" | "ha_supervisor" | "ha_os" | "ha_host" |
"ha_app")`, not `ha core logs`.

## CLI reference (not available via run_ha_command)

```
ha core update / ha os update / ha supervisor update / ha apps update <slug>
                       # Updates — handled by Pueo's update manager, never ad hoc
ha core logs / ha supervisor logs / ha apps logs <slug>
                       # Use read_logs instead
ha supervisor info     # Supervisor version and state (use ha os info / ha core info)
ha supervisor reload   # Reload Supervisor configuration (rarely needed)
ha apps start|stop <slug>
ha backups new / ha backups remove <slug> / ha backups info <slug>
ha network info / ha network update
ha os datadisk list
ha docker reset-storage  # Nuclear: wipe Docker storage, re-download on reboot (HAOS 18.3+)
```

**Note:** `ha apps` replaced `ha addons` in HA 2026.2.

**Backups:** Pueo's repair pipeline takes a backup automatically before any config write
(`apply_fix`). Never try to create or delete backups with `run_ha_command`. If the user
needs a manual backup, tell them to use Settings → System → Backups.

## Commands that do NOT exist — never recommend these

```
ha supervisor cleanup    # DOES NOT EXIST
ha cleanup purge         # DOES NOT EXIST
ha os prune              # DOES NOT EXIST
docker system prune      # docker CLI is unavailable in the HAOS SSH shell
```

## Common Patterns

**Check config, then restart** (two separate calls; never chain with `&&`):
```
run_ha_command("ha core check")
run_ha_command("ha core restart")
```

**Check whether an app is running:** `run_ha_command("ha apps info core_mosquitto")` and
read the `state` field.

## Error Handling

- `ha core check` exit code 1 = config invalid; parse stdout for the error message.
- Commands that time out usually mean the Supervisor is busy or restarting; retry after 30 s.
- After `ha core restart`, give HA 60–90 s, then `run_ha_command("ha core info")` to confirm
  it came back up.
