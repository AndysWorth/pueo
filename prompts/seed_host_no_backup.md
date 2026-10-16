---
signature: host:resolution_issue:no_current_backup
---
# no_current_backup — Supervisor requires a recent full backup

**What it means:** The HA Supervisor requires a recent *full* backup to be present on
the HA host. Automatic HA backups are *partial* — only the Core config. Pueo's own
backups are full, but the retention policy may have deleted them all from HA.

## Investigation

- `ha resolution info --raw-json` → confirm the issue type is `no_current_backup`.
- `ha backups list --raw-json` → check `type` field; count backups where `type == "full"`.

## Fix

1. If a full backup already exists on HA: apply the suggestion `create_full_backup` via
   `ha resolution suggestion apply <uuid>` (MEDIUM) to point the Supervisor at it, then
   `ha resolution healthcheck` (LOW) to clear the issue.
2. If no full backup exists on HA: the suggestion still applies — the Supervisor will
   create one after the healthcheck. Apply `create_full_backup` suggestion and confirm.
3. Set `outcome="resolved"` only after confirming via `ha resolution info --raw-json` that
   the issue UUID is no longer present.
