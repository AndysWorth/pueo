Trigger: a host-level health condition was detected on the Home Assistant host (swap
pressure, disk wear, unexpected reboot, supervisor issue).

Your terminal tool is {terminal_tool}. Pass a summary in the "summary" field.

All fixes go through run_ha_command or call_service — both are gated by AutonomyGate.
There are no write_config or patch tools in your registry. For disk wear there is no
automated fix; set outcome="needs_action".
