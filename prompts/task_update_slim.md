Trigger: a Home Assistant Core update is available and needs a safety assessment.

Your terminal tool is finish_update_analysis.

The release notes are already in the initial context, under
"## Pre-computed BC assessment results": the digest with a BC verdict table whose
per-BC assessments are already filled in. There is no tool to fetch release notes in this
session and none is needed. Use only the tools offered.

Steps:
1. Call get_pueo_command_catalog for minor/major releases and check for Pueo impacts.
2. Call finish_update_analysis:
   - Set safe_to_update, instance_impact from the verdict table (action_needed before_update
     → instance_impact=high; otherwise low or none as shown in the table).
   - Write summary: one sentence about the overall update status.
   - Write highlights: 2–5 feature changes from the screening section.
   - Set breaking_changes to BCs with verdict action_needed or user_check.
   - Do not supply bc_verdicts — it is not part of the tool in this session.
   Flags are informational only — instance_impact comes from BC verdicts, never from flags.

Set create_hitl_card=true.
