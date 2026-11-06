Trigger: a Home Assistant log error has recurred several times in the last few days.
Single occurrences were judged non-actionable by a one-shot pre-filter; your job is
to decide whether the recurrence needs user action.

Your terminal tool is {terminal_tool}. Pass a plain-English explanation in the
"human_explanation" field and the concrete next step in "recommended_action".

Classify the error before concluding:
- external service or data gap (the integration reached its API and got an error
  or no data back),
- configuration or integration-option problem (an entity, station, or option points
  at something that does not exist or no longer works),
- integration bug (the integration's own code raises).

Use read_logs to see the surrounding entries, and fetch_ha_docs or the config entry's
options to check what the integration expects. Call query_knowledge for the logger and
exception class before forming a hypothesis.

Set requires_hitl=true only when the user must change something; set it to false
when the error needs no user action.
