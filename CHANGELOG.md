# Changelog

All notable changes to Pueo are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning: [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Pre-1.0: the minor version is the breaking-change signal (0.1 → 0.2 = breaking).

## [Unreleased]

### Removed

- **Control tab** — the `/control` page and its "Run a Mode" buttons have been removed.
  Each function has moved to a better location:
  - *Service controls* → Settings tab (full state machine: install/start/stop/restart/uninstall
    with confirm on destructive actions and an install guard when Pueo is running outside launchd).
  - *NetAlertX diagnose* → Overview tab (card visible only when `NETALERTX_ENABLED=true`).
    Diagnosis now runs in-process via the supervisor work queue instead of spawning a detached
    `main.py` subprocess.
  - *Update check / RAG refresh* → Overview tab "Run now" buttons on the respective loop rows.
  - *Backup status* → Backups tab inventory + `backup_sync` loop "Run now".
  - *Quick links* → the navigation bar (already there).

  **Why:** the old "Run a Mode" buttons spawned detached `python main.py --mode <x>` child
  processes with output discarded. Those processes ran outside the supervisor, bypassed the
  `PueoWorkQueue` (ADR 025/026), and could create duplicate cards or leave orphaned processes
  waiting on approval. Dashboard actions must never spawn `main.py` subprocesses.

### Deprecated

- **`--mode audit`** — the standalone audit mode is deprecated and will be removed in the
  next major release (0.2.0). Its checks are covered live by the Overview tab (pending
  actions, loop health, resources), the Disk tab, and the Backups tab.
  A deprecation warning and a stderr line are now emitted when `--mode audit` is invoked.

## [0.1.0] — initial release
