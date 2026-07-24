# NetAlertX Diagnostic Skill

Perform a systematic read-only investigation of a running NetAlertX instance and its integration with Home Assistant. Use this skill when NetAlertX appears to be malfunctioning, missing device events, or not communicating with HA.

Run each check below in order. All checks are read-only — no writes, no config changes.

---

## Step 0 — Load credentials from config

Read `config.yaml` to extract:
- `home_assistant.host` → `HA_HOST`
- `home_assistant.ssh_key_path` → `SSH_KEY`
- `home_assistant.api_token` → `HA_TOKEN`
- `netalertx.api_token` → `NAX_TOKEN`
- `netalertx.api_port` (default 20212) → `NAX_PORT`

All curl and ssh commands below substitute these values.

---

## Step 1 — Connectivity

```bash
# SSH to HA
ssh -i $SSH_KEY -o StrictHostKeyChecking=no -o ConnectTimeout=10 root@$HA_HOST "echo SSH_OK"

# HA REST API
curl -s -H "Authorization: Bearer $HA_TOKEN" "http://$HA_HOST:8123/api/"

# NetAlertX API root (expect redirect to /docs)
curl -s "http://$HA_HOST:$NAX_PORT/"

# NetAlertX health (should return JSON with cpu_temp, mem_mb, etc.)
curl -s "http://$HA_HOST:$NAX_PORT/health" -H "Authorization: Bearer $NAX_TOKEN"

# NetAlertX metrics (Prometheus text; check netalertx_connected_devices)
curl -s "http://$HA_HOST:$NAX_PORT/metrics" -H "Authorization: Bearer $NAX_TOKEN"
```

**What to look for:**
- SSH must succeed. HA API must return `{"message":"API running."}`
- NetAlertX health must return JSON with `"success":true`
- Metrics must show `netalertx_connected_devices`, `netalertx_offline_devices`, `netalertx_new_devices`
- Check the counts: if `new_devices` equals total devices, notifications have NEVER succeeded

---

## Step 2 — Addon status

```bash
ssh -i $SSH_KEY -o StrictHostKeyChecking=no root@$HA_HOST \
  "ha apps list 2>&1 | grep -E 'netalertx|state|version'"

ssh -i $SSH_KEY -o StrictHostKeyChecking=no root@$HA_HOST \
  "ha apps info db21ed7f_netalertx_fa 2>&1 | grep -E 'state:|version:|network:|services:'"
```

**What to look for:**
- `state: started` — if `state: stopped` or `state: error`, addon is down
- `version` — current version
- `services: - mqtt:want` — NetAlertX requests MQTT service from HA
- Port mapping: `20211/tcp` = WebUI, `20212/tcp` = GraphQL & API
- **Note:** The addon slug is `db21ed7f_netalertx_fa`, NOT `netalertx_fa`

---

## Step 3 — API endpoint audit

Test each endpoint that Pueo uses. A 404 or Forbidden means that Pueo call is broken:

```bash
# REST endpoints (no /api/v1/ prefix — just the path directly)
curl -s "http://$HA_HOST:$NAX_PORT/devices" -H "Authorization: Bearer $NAX_TOKEN" | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print(f'devices: {len(d.get(\"devices\",[]))}')"
curl -s "http://$HA_HOST:$NAX_PORT/events" -H "Authorization: Bearer $NAX_TOKEN" | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print(f'events: {d.get(\"count\",\"ERR\")}')"
curl -s "http://$HA_HOST:$NAX_PORT/health" -H "Authorization: Bearer $NAX_TOKEN"
curl -s "http://$HA_HOST:$NAX_PORT/metrics" -H "Authorization: Bearer $NAX_TOKEN" | head -5

# /settings/<key> requires auth — KNOWN BUG: detector.py calls this WITHOUT auth
curl -s "http://$HA_HOST:$NAX_PORT/settings/VERSION" -H "Authorization: Bearer $NAX_TOKEN"
curl -s "http://$HA_HOST:$NAX_PORT/settings/VERSION"  # no auth — should return Forbidden

# GraphQL (port 20212)
curl -s -X POST "http://$HA_HOST:$NAX_PORT/graphql" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $NAX_TOKEN" \
  -d '{"query":"{ __schema { queryType { name } } }"}'

# scan trigger
curl -s -X POST "http://$HA_HOST:$NAX_PORT/nettools/trigger-scan" \
  -H "Authorization: Bearer $NAX_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"type":"ARPSCAN"}'
```

**What to look for:**
- `/devices`, `/events`, `/health`, `/metrics`, `/nettools/trigger-scan` should all work
- `/settings/VERSION` WITH auth → `{"success":true,"value":"v26.x.x"}`
- `/settings/VERSION` WITHOUT auth → `{"error":"Forbidden"}` (expected; detector.py has a bug here)
- GraphQL → `{"data":{"__schema":{"queryType":{"name":"Query"}}}}`

---

## Step 4 — Key NetAlertX settings

```bash
curl -s -X POST "http://$HA_HOST:$NAX_PORT/graphql" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $NAX_TOKEN" \
  -d '{"query":"{ settings { settings { setKey setValue } count } }"}' \
  | python3 -c "
import sys, json
result = json.loads(sys.stdin.read())
if 'errors' in result:
    print('GRAPHQL ERRORS:', result['errors'])
    exit()
settings = result.get('data',{}).get('settings',{}).get('settings',[])
print(f'Total settings: {len(settings)}')
keys_to_show = ['MQTT_RUN','MQTT_BROKER','MQTT_PORT','MQTT_USER','MQTT_PASSWORD',
                'SCAN_SUBNETS','TIMEZONE','ARPSCAN_RUN','ARPSCAN_RUN_SCHD',
                'BACKEND_API_URL','REPORT_DASHBOARD_URL','VERSION','API_TOKEN']
for s in settings:
    k = s.get('setKey','')
    if k in keys_to_show:
        print(f'  {k} = {s.get(\"setValue\",\"\")[:100]}')
"
```

**Critical things to check:**
- `MQTT_RUN` → must be `on_notification` or `always_after_scan`, NOT `disabled`
- `SCAN_SUBNETS` → must match your home LAN (e.g., `['10.0.0.0/24  eth0']`), NOT `172.30.32.0/23`
- `MQTT_BROKER` → should be `homeassistant.local` or HA's IP
- `BACKEND_API_URL` → if empty, no webhook notifications configured
- `REPORT_DASHBOARD_URL` → if still the placeholder string, not configured

---

## Step 5 — Device health

```bash
curl -s "http://$HA_HOST:$NAX_PORT/devices" \
  -H "Authorization: Bearer $NAX_TOKEN" \
  | python3 -c "
import sys, json
data = json.loads(sys.stdin.read())
devices = data.get('devices', [])
print(f'Total devices: {len(devices)}')

statuses = {}
for d in devices:
    st = d.get('devStatus','(empty)')
    statuses[st] = statuses.get(st, 0) + 1
print(f'devStatus distribution: {statuses}')

present = sum(1 for d in devices if d.get('devPresentLastScan'))
new = sum(1 for d in devices if d.get('devIsNew'))
print(f'Present in last scan (devPresentLastScan=1): {present}')
print(f'New/unacknowledged (devIsNew=1): {new}')

print('Present devices:')
for d in devices:
    if d.get('devPresentLastScan'):
        print(f'  {d.get(\"devMac\")} {d.get(\"devName\")} {d.get(\"devLastIP\")} last={d.get(\"devLastConnection\")}')
"
```

**What to look for:**
- `devStatus` is often empty (a known API quirk) — use `devPresentLastScan` for online status
- `devIsNew = 1` for all devices → notifications have never cleared; MQTT has never fired
- `devPresentLastScan = 0` for all LAN devices → wrong scan subnet

---

## Step 5.5 — WebUI loading spinner hang

If the NetAlertX WebUI shows a "Loading" spinner and stays dim indefinitely, the root cause is almost always `cacheDevices` failing — the JavaScript init sequence calls `php/server/query_json.php?file=table_devices.json`, parses it with `JSON.parse()`, and if that fails the spinner never hides.

**Check 1: does the JSON response parse cleanly?**

```bash
curl -s "http://$HA_HOST:20211/php/server/query_json.php?file=table_devices.json&nocache=1" | python3 -c "
import sys, json
text = sys.stdin.read()
if 'Infinity' in text or 'NaN' in text:
    idx = text.find('Infinity') if 'Infinity' in text else text.find('NaN')
    print('FAIL: non-standard JSON literal at position', idx)
    print('Context:', repr(text[max(0,idx-100):idx+50]))
else:
    try:
        d = json.loads(text)
        print(f'OK: {len(d.get(\"data\",[]))} devices, JSON valid')
    except json.JSONDecodeError as e:
        print('FAIL: JSON parse error:', e)
"
```

**If Infinity is found:** one device has a REAL-type field (usually `devName`) stored as SQLite floating-point infinity. PHP's `json_encode(INF)` emits bare `Infinity` which is not valid JSON.

**Check 2: find the bad device**

```bash
# SCP database to macOS
scp -i $SSH_KEY -o StrictHostKeyChecking=no \
  root@$HA_HOST:/addon_configs/db21ed7f_netalertx_fa/db/app.db /tmp/netalertx_app.db

python3 -c "
import sqlite3
conn = sqlite3.connect('/tmp/netalertx_app.db')
cur = conn.cursor()
cur.execute(\"SELECT devMac, devName, typeof(devName), devVendor, devLastIP FROM Devices WHERE typeof(devName) != 'text'\")
rows = cur.fetchall()
print(f'Non-text devName records: {len(rows)}')
for r in rows: print(r)
conn.close()
"
```

**Fix: repair the database**

The fix requires stopping the addon, applying the repair, deleting the WAL/SHM files, and restarting:

```bash
# 1. Fix the local DB copy (replace 'Canon Printer' with correct name or '' to let NAX auto-name)
python3 -c "
import sqlite3
conn = sqlite3.connect('/tmp/netalertx_app.db')
cur = conn.cursor()
cur.execute(\"UPDATE Devices SET devName = '' WHERE typeof(devName) != 'text'\")
conn.commit()
cur.execute('PRAGMA wal_checkpoint(TRUNCATE)')
print('Fixed rows:', conn.total_changes)
conn.close()
"

# 2. Stop addon
ssh -i $SSH_KEY -o StrictHostKeyChecking=no root@$HA_HOST "ha apps stop db21ed7f_netalertx_fa"

# 3. Upload fixed DB, delete WAL/SHM
scp -i $SSH_KEY -o StrictHostKeyChecking=no \
  /tmp/netalertx_app.db root@$HA_HOST:/addon_configs/db21ed7f_netalertx_fa/db/app.db
ssh -i $SSH_KEY -o StrictHostKeyChecking=no root@$HA_HOST \
  "rm -f /addon_configs/db21ed7f_netalertx_fa/db/app.db-wal /addon_configs/db21ed7f_netalertx_fa/db/app.db-shm"

# 4. Restart addon
ssh -i $SSH_KEY -o StrictHostKeyChecking=no root@$HA_HOST "ha apps start db21ed7f_netalertx_fa"

# 5. Wait for startup and verify
sleep 8
curl -s "http://$HA_HOST:20211/php/server/query_json.php?file=table_devices.json&nocache=1" \
  | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print(f'OK: {len(d[\"data\"])} devices')"
```

**Other known WebUI console errors (not the spinner cause):**
- `GET /api/hassio_ingress/<TOKEN>/server/messaging/in-app/unread 404` — `modal.js` uses `getApiBase()` which returns an ingress path when `BACKEND_API_URL` is empty. Harmless cosmetic error; fix by setting `BACKEND_API_URL` in NetAlertX settings.
- `SSE /server/sse/state → "Access Restricted"` — nginx on port 20211 blocks access to the FastAPI `/server/` prefix. SSE manager falls back to polling `app_state.json` after 3 attempts (15s delay). Cosmetic.

**Database location and tool availability:**
- DB path on HA host: `/addon_configs/db21ed7f_netalertx_fa/db/app.db`
- `sqlite3` CLI: **not available** on the HA SSH host
- `python3`: **not available** on the HA SSH host  
- Fix requires SCP to macOS where Python sqlite3 is available

---

## Step 6 — MQTT broker check

```bash
ssh -i $SSH_KEY -o StrictHostKeyChecking=no root@$HA_HOST \
  "ha apps info core_mosquitto 2>&1 | grep -E 'state:|version:'"

ssh -i $SSH_KEY -o StrictHostKeyChecking=no root@$HA_HOST \
  "ha apps logs core_mosquitto 2>&1" | tail -30
```

**What to look for:**
- Mosquitto should be `state: started`
- Look for repeated connect/disconnect cycles (could indicate bad credentials or connection loop)
- `error: received null username or password` → something connecting without credentials
- `not authorised` → wrong credentials
- `u'mqtttester'` connections → Pueo's MQTT health probe connecting

---

## Step 7 — NetAlertX addon logs

```bash
ssh -i $SSH_KEY -o StrictHostKeyChecking=no root@$HA_HOST \
  "ha apps logs db21ed7f_netalertx_fa 2>&1" | tail -80

# Check for errors specifically
ssh -i $SSH_KEY -o StrictHostKeyChecking=no root@$HA_HOST \
  "ha apps logs db21ed7f_netalertx_fa 2>&1" | grep -E "(ERROR|WARNING|MQTT|ARPSCAN.*YES|failed)" | head -30
```

**What to look for:**
- `[Scheduler] run for ARPSCAN: YES` → scan is triggering (if always NO, check schedule and timezone)
- `[MQTT]` entries → any MQTT activity
- `[Notification] No changes to report` → normal if no new/down devices
- `[HTTP]` entries → API calls being made (useful to see if Pueo is hitting the API)

---

## Step 8 — HA integration check

```bash
# Check NetAlertX-related entities
curl -s -H "Authorization: Bearer $HA_TOKEN" "http://$HA_HOST:8123/api/states" \
  | python3 -c "
import sys, json
states = json.loads(sys.stdin.read())
na = [s for s in states if 'netalertx' in s.get('entity_id','').lower()]
print(f'NetAlertX entities: {len(na)}')
for s in na:
    print(f'  {s[\"entity_id\"]} = {s[\"state\"]}')
    at = s.get('attributes', {})
    if 'last_triggered' in at:
        print(f'    last_triggered: {at[\"last_triggered\"]}')
"

# Check HA logbook for NetAlertX activity (48h)
curl -s -H "Authorization: Bearer $HA_TOKEN" "http://$HA_HOST:8123/api/logbook?hours_to_show=48" \
  | python3 -c "
import sys, json
entries = json.loads(sys.stdin.read())
na = [e for e in entries if 'netalertx' in str(e).lower()]
print(f'Logbook entries (48h): {len(entries)} total, {len(na)} NetAlertX-related')
for e in na[:10]:
    print(json.dumps(e))
"
```

**What to look for:**
- `automation.netalertx_event_handler` should have a non-null `last_triggered` — if null, no events have reached HA
- Zero NetAlertX logbook entries confirms complete integration failure

---

## Step 9 — HA automation config

```bash
ssh -i $SSH_KEY -o StrictHostKeyChecking=no root@$HA_HOST \
  "grep -A 30 'netalertx' /config/automations.yaml 2>&1"
```

**What to look for:**
- `webhook_id: 'netalertx_event'` — trigger webhook ID
- `local_only: true` — webhook only accepts local connections (OK for HA addon)
- Action field names: `trigger.json.eveMac`, `trigger.json.eveIp` etc. (camelCase, matching REST API)

---

## Step 10 — GraphQL API surface (if REST endpoints fail)

```bash
# Full schema
curl -s -X POST "http://$HA_HOST:$NAX_PORT/graphql" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $NAX_TOKEN" \
  -d '{"query":"{ __type(name: \"Query\") { fields { name } } }"}' | python3 -m json.tool

# Devices via GraphQL (correct syntax for v26.7.1+)
curl -s -X POST "http://$HA_HOST:$NAX_PORT/graphql" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $NAX_TOKEN" \
  -d '{"query":"{ devices(options: {page: 1, limit: 5}) { count dbCount devices { devMac devName devLastIP devPresentLastScan devIsNew devLastConnection } } }"}' \
  | python3 -m json.tool | head -60

# AppEvents via GraphQL (correct field names for v26.7.1+)
curl -s -X POST "http://$HA_HOST:$NAX_PORT/graphql" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $NAX_TOKEN" \
  -d '{"query":"{ appEvents(options: {page: 1, limit: 5}) { count appEvents { index dateTimeCreated objectType objectPlugin appEventType } } }"}' \
  | python3 -m json.tool
```

**API schema notes for v26.7.1:**
- `devices` (lowercase), not `Devices`
- `DeviceResult.devices` (list), `count`, `dbCount` — NOT `data` or `total`
- `PageQueryOptionsInput.page` and `.limit` are scalar Ints (not nested object)
- Device field for IP: `devLastIP` or `devPrimaryIPv4`, NOT `devIp`
- Device field for online status: `devPresentLastScan`, NOT `devIsPresent` or `devStatus`
- `AppEvent` fields: `index`, `guid`, `appEventProcessed`, `dateTimeCreated`, `objectType`, `objectPlugin`, `objectPrimaryId`, `appEventType`, `extra`
  — NOT `evtMac`, `evtIP`, `evtType`, etc.

---

## Diagnostic Summary Template

After running all checks, report:

```
CONNECTIVITY:       SSH ✓/✗  HA API ✓/✗  NetAlertX ✓/✗
ADDON STATE:        started/stopped/error, version x.x.x
SCAN SUBNET:        <value> — correct LAN? YES/NO
MQTT_RUN:           enabled/disabled
MQTT CONNECTION:    working/failing (Mosquitto logs)
DEVICES:            N total, M present last scan, K new/unacknowledged
WEBUI SPINNER:      loading OK / hung (check Step 5.5 — Infinity devName)
HA INTEGRATION:     automation last_triggered: <timestamp or null>
NOTIFICATION PATH:  MQTT / webhook / none configured
BROKEN PUEO CODE:   list of issues in Pueo code vs live API
```
