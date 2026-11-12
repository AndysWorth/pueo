Trigger: a Home Assistant update is available and needs a safety assessment.

Your terminal tool is finish_update_analysis.

Steps:
1. Call get_update_release_notes to get the pre-built digest.
   The digest contains a BC verdict table with per-BC assessments already filled in.
   Flags are informational only — do not raise instance_impact from them.
2. Call get_pueo_command_catalog for minor/major releases and check for Pueo impacts.
3. Call finish_update_analysis with the recommendation and bc_verdicts from the digest.
   instance_impact is derived from BC verdicts only — ignore flags.

Never claim there are no breaking changes without reading the release notes. Set
create_hitl_card=true for all core/os/supervisor updates.
