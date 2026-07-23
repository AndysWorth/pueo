# Item 28 — MQTT Credential Setup

## Goal

Ensure that when Mosquitto requires authentication, Pueo collects the credentials
during setup and applies them to NetAlertX's `app.conf` automatically — so the
broker connection works without manual `app.conf` editing.

## Problem

Three gaps prevented end-to-end MQTT authentication:

1. `setup.sh` never asked for MQTT credentials and wrote them only as commented-out
   hints in `config.yaml`.
2. `installer.py` step 6 wrote `MQTT_BROKER` and `MQTT_PORT` to `app.conf` but never
   wrote `MQTT_USER` or `MQTT_PASSWORD`, so credentials sat unused in `config.yaml`.
3. `installer.py` step 7 HITL body hardcoded "no credentials" in its manual HA MQTT
   integration instructions regardless of config.

## Solution

- **`setup.sh`**: added a Mosquitto health check (`ha addons info core_mosquitto`)
  and interactive prompts for `MQTT_USER` / `MQTT_PASSWORD`; writes them as active
  keys (not comments) in `config.yaml`.
- **`installer.py` step 6**: conditionally adds `MQTT_USER` and `MQTT_PASSWORD` to
  the `app.conf` merge when `NETALERTX_MQTT_USER` is non-empty; omits them for
  anonymous access.
- **`installer.py` step 7**: HITL body now includes the configured credentials (or
  "no credentials") in the manual HA MQTT integration setup instructions.

## Files Changed

- `setup.sh`
- `netalertx/installer.py`
- `tests/test_netalertx.py` — two new tests in `TestNetAlertXInstallerSteps5to8`

## Done Criteria

- `setup.sh` prompts for MQTT credentials and writes them to `config.yaml`
- `--mode netalertx-setup` step 6 writes `MQTT_USER`/`MQTT_PASSWORD` to `app.conf`
  when credentials are configured
- `--mode netalertx-diagnose` reports `mqtt_active=True` after setup on an
  authenticated Mosquitto instance
- All 612 tests pass; coverage ≥ 90%

## Status

✅ Done (2026-07-23)
