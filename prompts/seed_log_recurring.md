---
signature: log:*:*
---
# Recurring log error — method

Trigger: the same log error (same logger and exception) recurred several times within
the recurrence window.

## Investigation

1. `read_logs(lines=200, log_source="ha_core")` — find every occurrence and its timing.
   A fixed time of day points at a scheduled poll; bursts point at an outage.
2. Identify the integration from the logger (`homeassistant.components.<domain>` or
   `custom_components.<domain>`) and read its config entry or YAML entry.
3. `fetch_ha_docs("<domain>", "sensor.py")` (or the platform named in the traceback) to see
   which API call raises and what it expects from the configuration.
4. If the error names an external API, `fetch_url` the endpoint to check whether it is up.

## Decision rules

- The API answers but returns "no data" for the configured station, datum, location or
  product → configuration/option change needed (the station does not publish that data).
  `requires_hitl=true`; recommend the specific option change.
  Example: noaa_tides `COOPSAPIError: ... No data was found` → the station does not offer
  the configured datum/product; choose a station or datum that publishes predictions.
- Connection errors, timeouts, 5xx or name-resolution failures that clear on their own
  → transient external outage. `requires_hitl=false`.
- A traceback whose innermost frame is in `custom_components/<domain>/` → integration bug;
  recommend checking for an update or reporting upstream.
- A core integration that raises an unexpected exception on valid configuration → likely
  an upstream bug; recommend checking the integration's open issues.
