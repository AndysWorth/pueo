Trigger: a Home Assistant device has been offline beyond its grace period.

Your terminal tool is {terminal_tool}. Pass a summary in the "summary" field.

The only automated fix available is reload_integration, which is gated by AutonomyGate.
There are no write_config or patch tools in your registry. If root cause is a vendor
outage or local power/network issue, set outcome="monitoring" or outcome="needs_action".
