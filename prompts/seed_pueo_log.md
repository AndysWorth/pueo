# Pueo Log Investigation

Trigger: errors in Pueo itself, stream resets, loop crashes, agent loop failures, Pueo not working

## Approach

1. Call read_pueo_log with level="ERROR" or level="WARNING" to find recent issues in Pueo's
   own structured JSON log.
2. Call search_log("pueo", pattern="<keyword>") if looking for a specific error or event by name.
3. Look for patterns: repeated exceptions, loop restart messages, LLM timeout entries, DB errors.
4. Call finish_chat with what the log shows — the specific error, when it started, and whether
   it is a known transient issue or a sign of a configuration or connectivity problem.
