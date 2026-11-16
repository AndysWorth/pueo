Trigger: a Home Assistant Core update is available and needs a safety assessment.

Your terminal tool is finish_update_analysis.

The release notes are already in the initial context, under
"## Pre-computed BC assessment results": the digest with a BC verdict table whose
per-BC assessments are already filled in. There is no tool to fetch release notes in this
session and none is needed. Use only the tools offered.

Steps:
1. Call get_pueo_command_catalog for minor/major releases and check for Pueo impacts.
2. Call finish_update_analysis:
   - Write summary: one sentence about the overall update status.
   - Write highlights: 2–5 feature changes from the screening section.
   - Set pueo_command_risks to Pueo commands the release notes change (empty if none).
   The card's safety verdict, impact and breaking-change list are filled in from the
   verdict table by code. Do not restate or re-judge the BCs, and do not supply fields
   the tool does not list.

Set create_hitl_card=true.
