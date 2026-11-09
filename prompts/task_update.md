Trigger: a Home Assistant update is available and needs a safety assessment.

Your terminal tool is finish_update_analysis. Pass a recommendation and bc_verdicts
(one entry per BC in the digest) in the terminal call.

Steps:
1. Call get_update_release_notes to get the pre-built digest with Evidence blocks.
2. For each BC in the digest, judge from its Evidence block: yes / no / unknown.
   Call check_config_against_breaking_change only when Evidence missed a specific key.
3. Call get_pueo_command_catalog for minor/major releases and check for Pueo impacts.
4. Call finish_update_analysis with bc_verdicts covering every BC.

Never claim there are no breaking changes without reading the release notes. Set
create_hitl_card=true for all core/os/supervisor updates.
