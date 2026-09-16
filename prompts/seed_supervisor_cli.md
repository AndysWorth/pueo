# HA Supervisor CLI Reference

Trigger: run_ha_command, supervisor CLI, ha apps, ha core, ha os, ha backups, ha supervisor,
ha network, which CLI command, how to restart HA, how to update HA, how to list backups

## Overview

The `run_ha_command` tool executes these commands on the HA host via SSH. All commands use
the `ha` CLI provided by the Home Assistant Supervisor.

## Core Commands

```
ha core check          # Validate configuration.yaml (returns errors; exit 0 = valid)
ha core restart        # Restart Home Assistant Core (brief outage, keeps add-ons running)
ha core update         # Update HA Core to the latest available version
ha core logs           # Show recent Core log lines (use --follow for live tail)
ha core info           # Show current Core version, state, and boot count
```

## OS Commands

```
ha os update           # Update HA OS to the latest available version
ha os info             # Show OS version, board, and boot slot
```

## Apps (formerly Add-ons)

```
ha apps list           # List all installed apps with slug, state, and version
ha apps info <slug>    # Show details for one app (e.g. ha apps info core_mosquitto)
ha apps start <slug>   # Start an app
ha apps stop <slug>    # Stop an app
ha apps restart <slug> # Restart an app
ha apps update <slug>  # Update one app to latest
ha apps logs <slug>    # Show recent logs for an app
```

**Note:** `ha apps` replaced `ha addons` in HA 2026.2. Use `ha apps` on any installation
running 2026.2 or later.

## Backups

```
ha backups list                    # List all backups with slug, name, date, size
ha backups new --name "my-backup"  # Create a full backup snapshot (returns slug)
ha backups remove <slug>           # Delete a backup by slug
ha backups info <slug>             # Show details for one backup
```

**Safety rule:** Always run `ha backups new` and confirm the returned slug before any
write operation on configuration.yaml. Never skip this step.

## Supervisor

```
ha supervisor logs     # Show Supervisor log lines
ha supervisor reload   # Reload Supervisor configuration (rarely needed)
ha supervisor update   # Update the Supervisor itself
ha supervisor info     # Show Supervisor version and state
```

## Network

```
ha network info        # Show current network config (IPs, interfaces, DNS)
ha network update      # Update network config (requires JSON args; prefer HA UI)
```

## Disk / Maintenance

```
ha os datadisk list    # List available data disks (for migration)
```

## Common Patterns

**Check config and restart:**
```
ha core check && ha core restart
```

**Create a backup and capture the slug:**
```
ha backups new --name "pre-repair-$(date +%Y%m%d)"
# Note the slug from the output before proceeding
```

**Check if an app is running:**
```
ha apps info core_mosquitto | grep state
```

**View live HA log:**
```
ha core logs --follow
```

## Error Handling

- `ha core check` exit code 1 = config invalid; parse stdout for the error message.
- Commands that time out usually mean the Supervisor is busy or restarting; retry after 30 s.
- If `ha core restart` returns immediately with no output, the restart was queued; give it
  60–90 s before checking `ha core info` to confirm it came back up.
