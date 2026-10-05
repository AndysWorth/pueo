# HA Disk Space Investigation

Trigger: disk space low, HA disk usage, backups taking too much space, recorder DB large

## Approach

1. Call get_disk_usage to get current free space, total size, and breakdown by category.
2. Optionally call run_ha_command("ha backups list") to see backup count and sizes.
3. If the recorder DB is large, consider recommending purge or retention config changes.
4. If backups are the dominant consumer, recommend offloading older backups or adjusting
   the retention policy.
4a. If "OS + container images" is the dominant category (common on HA Yellow after frequent
    updates):
    - The correct cleanup command is: `ha supervisor repair`
      This triggers the Supervisor to remove unused container image layers. Safe; no config
      or data loss. Recommend rebooting afterward.
    - For severe cases (corrupted images, HAOS 18.3+): `ha docker reset-storage`
      Nuclear: wipes ALL Docker storage and re-downloads everything on next reboot. Requires
      internet connectivity. Only recommend if images are corrupted or space is critically low
      and the user understands it will trigger a full re-download.
    - Do NOT recommend: `ha supervisor cleanup` (does not exist), `docker system prune`
      (docker CLI is unavailable in the HAOS SSH shell).
5. Call finish_chat with specific, actionable advice based on the real numbers — not generic
   advice. Include the actual free space figure and the dominant space consumer.
