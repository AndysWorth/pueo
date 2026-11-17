Trigger: a Home Assistant Core update is available and needs a safety assessment.

Your terminal tool is finish_update_analysis.

The release notes are already in the initial context, under
"## Pre-computed BC assessment results": the digest with a BC verdict table whose
per-BC assessments are already filled in. There is no tool to fetch release notes in this
session and none is needed. Use only the tools offered.

Steps:
1. Call finish_update_analysis:
   - Write summary: one sentence about the overall update status.
   - Write highlights: 2–5 feature changes from the screening section.
   The card's safety verdict, impact, breaking-change list and Pueo impact are
   filled in from the pre-computed verdicts by code. Do not restate or re-judge
   the BCs, and do not supply fields the tool does not list.

Set create_hitl_card=true.
