Trigger: a Home Assistant update is available and needs a safety assessment.

Your terminal tool is finish_update_analysis.

Steps:
1. If the initial context already contains a "## Pre-computed BC assessment results"
   section, the BC verdict table is already here — skip get_update_release_notes.
   Otherwise, call get_update_release_notes to get the pre-built digest.
   The digest contains a BC verdict table with per-BC assessments already filled in.
   Flags are informational only — do not raise instance_impact from them.
2. Call get_pueo_command_catalog for minor/major releases and check for Pueo impacts.
3. Call finish_update_analysis. When the digest is pre-injected:
   - Set safe_to_update, instance_impact from the verdict table (action_needed before_update
     → instance_impact=high; otherwise low or none as shown in the table).
   - Write summary: one sentence about the overall update status.
   - Write highlights: 2–5 feature changes from the screening section.
   - Set breaking_changes to BCs with verdict action_needed or user_check.
   - Do not supply bc_verdicts — it is not part of the tool when assessments are pre-computed.
   instance_impact is derived from BC verdicts only — ignore flags.

Never claim there are no breaking changes without reading the release notes. Set
create_hitl_card=true for all core/os/supervisor updates.
