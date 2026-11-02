---
signature: setup:integration:noaa_it_all
---
# noaa_it_all (NOAA It All) — setup

**What it is:** a HACS integration (dawg-io/noaa_it_all) for NWS weather, alerts and
radar, plus surf, Great Lakes marine, space weather and hurricanes. UI config flow
only, no API key, `cloud_polling`. The minimum HA version is in `hacs.json`
(2026.3.0 as of v0.11.1). A `noaa_it_all:` block in `configuration.yaml` creates
nothing and logs an error: remove it, don't fix it.

## Flow (v0.11.x step ids)

1. `user`: a **menu** with `location | surf | marine | space | hurricane`.
   **Choosing `hurricane` creates the hurricane entry immediately.**
2. `location`: `latitude`, `longitude`, defaulting to the HA Home zone.
3. `office`: `office_code`. The office NWS assigns to the point is listed first, plus
   coastal offices within 50 miles. Submitting this step claims the unique_id
   `noaa_<OFFICE>_<lat 4dp>_<lon 4dp>` and aborts `already_configured` if any entry
   (including a disabled one) has it.
4. `surf`: `surf_office_code`, a coastal office or "No surf data".
5. `space`: `space_office_code`, this office or "No space data". **Submitting it
   creates the Weather entry** and, by import, the Surf entry (if one was chosen), the
   Space entry (if this office was chosen and not yet added), and `NOAA Hurricane` (on
   the integration's first entry only). One flow can therefore create **up to 4
   entries**. Verify each one.

## Decision rules

- **Office:** check it with `fetch_url("https://api.weather.gov/points/<lat>,<lon>")`.
  `properties.gridId` is the office and `radarStation` is the radar site. Choose
  `gridId` unless the user wants another office.
- **Surf:** only for coastal offices. Inland homes take "No surf data". "No forecast
  this week" next to an office means NWS issued no surf product in the last 7 days.
- **Space:** one Space entry per office. Add it with the first location unless the user
  doesn't want 20 space-weather entities.
- **Hurricane:** added once automatically. If the user deletes it, nothing adds it back
  except the `hurricane` menu option.
- **Marine:** Great Lakes nearshore zones only. Ocean and open-lake zones are not
  supported.

## Existing entry (common: "downloaded but not loaded")

- An entry with no `entry_type` in its data is **version 1** (from before 0.8).
  Enabling it runs `async_migrate_entry`, which splits it into Weather, Surf and
  Hurricane entries and keeps entity IDs and history.
- If the user prefers a clean re-add: delete the old entry first. Otherwise step 3
  aborts `already_configured`. Entity history and device areas are lost, so plan to
  re-assign the area. Check the new entity IDs before telling the user that dashboards
  and automations still work. Compare them with `check_automation_references`.

## Options (Weather entries only)

Options covers `latitude`/`longitude`, `office_code`, and `radar_loop_hours` (0–24,
default 24).
- `0` uses NOAA's own ~50-minute loop and stores nothing.
- `1`–`24` stores up to 72 frames under `/config/noaa_it_all/radar_frames/<site>/`, and
  every open dashboard re-downloads the loop.
- Suggest 6–12 hours on small hosts or for mobile viewers.

Surf, Marine and Space entries have no options. To change one, delete it and add it
again. A Space entry does not follow a Weather entry's office change.

## Verify

Expect devices such as `NOAA <OFFICE> Weather`, and entities such as
`sensor.noaa_<office>_weather_temperature` with a numeric state after the first poll.
Also expect `NOAA Hurricane` on a first install. Errors log under
`custom_components.noaa_it_all`.
