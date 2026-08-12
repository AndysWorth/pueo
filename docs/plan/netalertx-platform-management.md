# NetAlertX Platform Management

Part of the [Implementation Plan](../implementation-plan.md) · Phase 25 · 4 sessions.

**Context:** NetAlertX Full Access (`db21ed7f_netalertx_fa`) is installed on HA Yellow as an
add-on and is consuming enough disk space to breach Pueo's CRITICAL threshold (≤ 3.0 GB free).
This plan covers lifecycle management: uninstalling from HA, installing on a separate Docker
host, disk-space guards, full MQTT topic coverage, and hardening the installer to refuse the
non-FA variant unconditionally.

---

## Session A — Uninstall from HA + FA-only enforcement ✅ Complete (2026-08-12) — PR #210 (https://github.com/AndysWorth/pueo/pull/210)

**Branch:** `feat/209-netalertx-uninstall-fa-enforcement`  **Issue:** #209

| Item | Concern |
| ---- | ------- |
| A-1  | FA-only guard: `_parse_slug_from_store` + `_require_fa_slug` — refuse non-FA slugs |
| A-2  | `netalertx/uninstaller.py` — `NetAlertXUninstaller` with 4-step reverse state machine |
| A-3  | `--mode netalertx-uninstall` CLI entry point |
| A-4  | `CARD_TYPE_NETALERTX_UNINSTALL` HITL card (CRITICAL risk) |
| A-5  | `setup.sh` fix: `ha addons info core_mosquitto` → `ha apps info core_mosquitto` |
| A-6  | Tests: uninstaller state machine, FA guard, slug rejection, card registration |

**Done when:** `--mode netalertx-uninstall` removes the HA add-on and webhook automation;
`netalertx_install_state = NOT_INSTALLED`; installer refuses non-FA slug with a clear error
message; `ha addons` removed from `setup.sh`; CI passes.

---

## Session B — Separate-Machine Docker Installer ✅ Complete (2026-08-12) — PR #212 (https://github.com/AndysWorth/pueo/pull/212)

**Branch:** `feat/211-netalertx-docker-installer`  **Issue:** #211

New config keys (triple-update rule):

```yaml
netalertx:
  deploy_target: ha            # ha | docker
  docker_host: ""              # IP/hostname of separate machine
  docker_ssh_user: ""          # SSH user for Docker host
  docker_ssh_key_path: ""      # defaults to HA SSH key if blank
  docker_config_path: /opt/netalertx/config
  docker_image: ghcr.io/jokob-sk/netalertx:latest
  docker_min_disk_gb: 5.0
```

| Item | Concern |
| ---- | ------- |
| B-1  | `netalertx/docker_installer.py` — `DockerInstaller` 10-step state machine (SSH + Docker) |
| B-2  | MQTT routing: configure HA Mosquitto to accept external connections via `/ssl/mosquitto_custom.conf` + `ha apps restart core_mosquitto` (HITL, HIGH risk) |
| B-3  | `detector.py` extension — when `deploy_target=docker`, SSH to `docker_host` instead of HA |
| B-4  | `--mode netalertx-docker-setup` CLI entry point |
| B-5  | Tests: Docker installer, disk-too-low abort, MQTT routing card |

Docker run command:
```bash
docker run -d --name netalertx --restart=unless-stopped \
  --network=host --cap-add=NET_RAW \
  -v {docker_config_path}:/app/config \
  -p 20212:20212 {docker_image}
```

**Done when:** `--mode netalertx-docker-setup` installs NetAlertX FA on a separate Linux host;
`GET http://{docker_host}:20212/api/health` → 200; MQTT probe passes; webhook automation present.

---

## Session C — Disk Space Checks + setup.sh Improvements

**Issue:** TBD  **Branch:** `feat/<N>-netalertx-setup-improvements`

| Item | Concern |
| ---- | ------- |
| C-1  | `netalertx/disk_check.py` — `check_target_disk_space(ssh, path, min_gb)` + `DiskSpaceTooLowError` |
| C-2  | `setup.sh`: ask deploy target (HA vs separate machine), SSH details, disk check via `df -BG` |
| C-3  | Supervisor: disk CRITICAL + `deploy_target=ha` + NAX `FULLY_OPERATIONAL` → `CARD_TYPE_NETALERTX_MIGRATE` |
| C-4  | `config.yaml.default`: add all Session B keys with comments |
| C-5  | Tests: `DiskSpaceTooLowError`, migration card surfaces and suppressed when not applicable |

**Done when:** both installers call `check_target_disk_space()` and abort cleanly; setup.sh
prompts for deploy target and checks disk space; supervisor raises migration card when HA is
disk-critical and NAX is on HA.

---

## Session D — MQTT Full Integration + NetAlertX Capabilities

**Issue:** TBD  **Branch:** `feat/<N>-netalertx-mqtt-full`

| Item | Concern |
| ---- | ------- |
| D-1  | Expand `mqtt_subscriber.py`: `NetAlertX/alert/+`, `NetAlertX/device/+/state`, `NetAlertX/scan/complete` |
| D-2  | Typed event dataclasses: `NewDeviceAlertEvent`, `DeviceStateEvent`, `ScanCompleteEvent` |
| D-3  | `CARD_TYPE_NETALERTX_NEW_DEVICE` HITL card (LOW risk, notify-only; auto-approve at autonomy ≥ 4) |
| D-4  | `netalertx/event_router.py` — `NetAlertXEventRouter`: merge + deduplicate MQTT and webhook streams |
| D-5  | Tests + eval scenario `11_netalertx_new_device_mqtt.yaml` |

**Done when:** Pueo receives all NetAlertX FA MQTT topics; new-device detection creates a HITL
card; MQTT and webhook streams are deduplicated; CI passes.

---

## Related-File Checklist

| Change type | Files to update |
| ----------- | --------------- |
| New config key | `config.py`, `config.yaml.default`, `setup.sh` |
| New CLI mode | `main.py` (choices + dispatch + epilog), `tests/test_core_agent.py` (mode recognized) |
| New HITL card type | `utils/card_types.py`, `web/templates/index.html` (is_actionable + elif hint), `web/dashboard.py` (dispatch table if execute handler needed) |
| New netalertx module | `tests/test_netalertx.py` |

---

## End-to-End Verification

After all four sessions:

1. `python main.py --mode netalertx-uninstall` → add-on gone from HA; state = `NOT_INSTALLED`; `df -h` shows freed space
2. `python main.py --mode netalertx-docker-setup` → container running on separate machine; health endpoint 200; MQTT probe passes
3. `pueo monitor` (supervisor mode) → NAX log tail connects to Docker host; HA disk alerts not triggered by NAX
4. New device joins → MQTT `NetAlertX/alert/+` event → HITL card in dashboard
5. Simulate HA disk CRITICAL with `deploy_target=ha` (unit test) → migration card surfaces
6. Full CI: `black --check . && flake8 ... && mypy ... && bandit ... && pytest --cov --cov-fail-under=90 --ignore=tests/integration`
