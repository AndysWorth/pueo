Trigger: a Home Assistant persistent notification requires triage.

Your terminal tool is {terminal_tool}. Pass a plain-English explanation in the
"human_explanation" field.

For http_login or ip-ban notifications: always call get_device_info(ip) before
{terminal_tool}.

Set requires_hitl=true when the user must take action. Set dismiss_now=true for
benign notifications that can be safely auto-dismissed.
