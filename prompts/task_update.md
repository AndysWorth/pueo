Trigger: a Home Assistant update is available and needs a safety assessment.

Your terminal tool is finish_update_analysis. Pass a recommendation in the
"recommendation" field.

Two-pass method (mandatory):
1. Enumerate: call get_update_release_notes and list ALL breaking changes, deprecated
   settings, and removed APIs before verifying any of them.
2. Verify: call check_config_against_breaking_change for each item against this install.

Never claim there are no breaking changes without reading the release notes. Set
create_hitl_card=true for all core/os/supervisor updates.
