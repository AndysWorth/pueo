# HA Disk Space Investigation

Trigger: disk space low, HA disk usage, backups taking too much space, recorder DB large

## Approach

1. Call get_disk_usage to get current free space, total size, and breakdown by category.
2. Optionally call run_ha_command("ha backups list") to see backup count and sizes.
3. If the recorder DB is large, consider recommending purge or retention config changes.
4. If backups are the dominant consumer, recommend offloading older backups or adjusting
   the retention policy.
5. Call finish_chat with specific, actionable advice based on the real numbers — not generic
   advice. Include the actual free space figure and the dominant space consumer.
