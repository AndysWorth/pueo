# HA Notification Intelligence

Part of the [Roadmap](../roadmap.md) · Phase 10.5.

---

### Problem

Home Assistant surfaces security events, integration failures, and system state changes as persistent notifications — but Pueo currently ignores them. A failed login attempt, a broken integration, or a misconfigured add-on generates a notification that sits unread in the HA UI with no explanation of severity or recommended action. Many notifications contain raw technical data (IP addresses, entity IDs, error codes) that require context to interpret.

---

### Architecture notes

**How HA persistent notifications work.** HA creates a state entity for each notification with `entity_id = f"persistent_notification.{notification_id}"`. State value is `"notifying"`. Attributes include `message`, `title`, and `notification_id`. These entities are accessible via `GET /api/states` — no WebSocket required. The `HARestClient` introduced in item 62 handles this polling.

**Known notification IDs.** The only system `notification_id` values documented by HA are:
- `http_login` — invalid authentication / failed login attempt
- `invalid_config` — configuration error detected

All other notifications use integration-specific or user-defined IDs. Unknown IDs are handled generically.

**Notification vs update entities.** HA 2022.4+ surfaces update availability as `update.*` entities (item 62), not persistent notifications. This phase handles `persistent_notification.*` entities only.

**Dismissal.** `POST /api/services/persistent_notification/dismiss` with body `{ "notification_id": "<id>" }` clears the notification from the HA UI. Pueo only dismisses after explicit HITL approval — never automatically.

**IP enrichment.** For security notifications containing IP addresses, Pueo attempts three enrichment sources in order:
1. Reverse DNS (`socket.gethostbyaddr()`) from Pueo's machine — fast, local
2. NetAlertX device list (`GET /devices`) — matches IP to a friendly device name if NetAlertX is installed
3. HA device registry via WebSocket `config/device_registry/list` — matches `["ip", "<addr>"]` connections entry

All three are best-effort; enrichment failure does not block the HITL card.

**Deduplication.** Notifications already presented as HITL cards are tracked by `notification_id` in a new SQLite table (`notification_history`). A notification is not re-presented unless it reappears after having been dismissed.

---

### Feature 1 — Notification Polling + Triage (item 67)

**Polling.** A new periodic co-routine in the monitor loop polls `GET /api/states` for entities prefixed `persistent_notification.` every `HA_NOTIFICATION_POLL_INTERVAL_MINUTES`. Compares against `notification_history` to find newly appeared notifications.

**New config keys:**

| Key | Default | Meaning |
|-----|---------|---------|
| `HA_NOTIFICATION_POLL_INTERVAL_MINUTES` | `5` | How often to check for new notifications |
| `HA_NOTIFICATION_ENRICH_AUTH_FAILURES` | `true` | Enable IP enrichment for `http_login` |

**New Pydantic schema `NotificationAnalysis`:**
```python
class NotificationAnalysis(BaseModel):
    notification_id: str
    category: Literal["security", "update", "config_error", "integration", "other"]
    severity: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    original_title: str | None
    original_message: str
    human_explanation: str      # LLM plain-English explanation
    enriched_context: dict      # IP → hostname, device name, etc.
    recommended_action: str     # LLM recommendation
    requires_hitl: bool
```

**Category assignment (rule-based, no LLM):**
| `notification_id` | Category | Severity |
|---|---|---|
| `http_login` | `security` | `HIGH` |
| `invalid_config` | `config_error` | `HIGH` |
| Matches `update.*` | routed to item 62 | — |
| Anything else | `other` | `MEDIUM` |

**New SQLite table `notification_history`:**
```sql
CREATE TABLE notification_history (
    notification_id TEXT PRIMARY KEY,
    first_seen_at   REAL,
    last_seen_at    REAL,
    category        TEXT,
    severity        TEXT,
    hitl_sent_at    REAL,
    dismissed_at    REAL,
    dismissed_by    TEXT   -- 'user' or 'pueo'
);
```

---

### Feature 2 — Notification Enrichment (item 68)

**`http_login` — failed authentication enrichment.** Extract source IP from message using the pattern `r"from (\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})"`. Then:

1. **Reverse DNS** — `socket.gethostbyaddr(ip)` on Pueo's machine. Returns hostname or raises `herror`; catch and continue.
2. **NetAlertX lookup** — `GET /devices` filtered for `devLastIP == ip`. Returns device name (`devName`) and last-seen timestamp. Only runs if NetAlertX is configured and reachable.
3. **HA device registry** — WebSocket `config/device_registry/list`, filter `connections` for `["ip", ip]`. Returns HA device `name` and `name_by_user`.

Enriched context example:
```python
{
    "source_ip": "192.168.1.42",
    "hostname": "android-a1b2c3d4.local",
    "netalertx_name": "Andy's Phone",
    "ha_device_name": "Andy's Phone",
    "is_known_device": True
}
```

`is_known_device` is `True` if any enrichment source matched. If `is_known_device` is `False`, the HITL card flags it more urgently as a potentially external source.

**`invalid_config` — configuration error enrichment.** Include the current `configuration.yaml` content in the LLM context (truncated to token budget) so the explanation can cite the specific broken section.

**General enrichment.** For all notifications, the LLM receives: the raw notification title and message, any enriched context, and a request to explain in plain English what happened and what the user should do. `temperature=0.0`, structured output via `NotificationAnalysis`.

**WebSocket client.** The HA device registry lookup requires a short-lived WebSocket connection. New `HAWebSocketClient` in `utils/ha_ws_client.py`, implementing `HAWebSocketClientProtocol` (in `interfaces.py`). Used only for device registry lookup; closed immediately after the response is received. `FakeHAWebSocketClient` for tests. If the WebSocket connection fails, skip device registry enrichment and continue.

---

### Feature 3 — HITL Notification Cards + Dismissal (item 69)

**One HITL card per notification.** The card shows:
- Notification title and original message
- Category badge and severity indicator
- Enriched context (IP details, device name, etc.)
- LLM plain-English explanation
- LLM recommended action
- Two buttons: **Dismiss in HA** (calls dismiss service + marks `dismissed_by = 'user'`) and **Keep** (closes card without dismissing; notification stays in HA UI)

**`--mode notifications`** — one-shot: polls for all current `persistent_notification.*` entities, enriches and triages all of them, sends HITL cards for any not already in `notification_history`. Exits after cards are sent.

**Risk level for dismissal.** Dismissing a notification is LOW risk (reversible — the notification reappears on next HA restart if the underlying condition persists). Auto-dismissal is never performed; the button triggers the dismiss service call only after user clicks it.

**`http_login` special handling.** If `is_known_device = False`, severity is elevated to `CRITICAL` in the HITL card and the card subject line includes "⚠ Unknown source IP". If `is_known_device = True`, severity stays `HIGH` but the device name appears prominently ("Login attempt from Andy's Phone").

---

### Feature 4 — Notification History in Dashboard (item 70)

New **Notifications** tab in the HITL web dashboard (`web/dashboard.py`).

**Tab sections:**

| Section | Content |
|---------|---------|
| Pending | Notifications currently showing in HA UI, not yet dismissed; each has inline HITL card |
| History | All past notifications from `notification_history`; sortable by first_seen, category, severity |
| Detail view | Click any history row to expand: original message, enriched context, LLM explanation |

**Filters:** by category (security / config / integration / other) and by severity.

**`--mode notifications` output** also prints a summary table to stdout for non-dashboard use.

---

### Done when

- Monitor loop polls for `persistent_notification.*` entities every `HA_NOTIFICATION_POLL_INTERVAL_MINUTES`
- `http_login` notifications are enriched with reverse DNS + NetAlertX name + HA device registry lookup
- Unknown-source login attempts are escalated to CRITICAL in the HITL card
- All notifications receive an LLM plain-English explanation and recommended action
- Dismissal fires `POST /api/services/persistent_notification/dismiss` only on explicit user action
- `notification_history` SQLite table tracks all seen notifications and dismissal state
- HITL dashboard has a Notifications tab with pending and history views
- `--mode notifications` works as a one-shot CLI entry point
- `FakeHARestClient` and `FakeHAWebSocketClient` used in all unit tests
- All new config keys have tests in `TestConfigDefaults`
- Migration for `notification_history` table tested against real `ha_agent_state.db`
