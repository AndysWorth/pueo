# ADR 004 — SSH `known_hosts=None` for local-network HA hosts

## Status
Accepted

## Context
`asyncssh.connect()` requires either a path to a `known_hosts` file or explicit host key verification to protect against MITM attacks. Pueo connects to a Home Assistant instance on the local network (typically a single dedicated host). The user configures `HA_HOST` manually; the host does not change between connections.

## Decision
All `asyncssh.connect()` calls use `known_hosts=None`, disabling host key verification. This is intentional and documented.

## Rationale
- The HA host is user-configured and operates on a private LAN — the MITM threat model applicable to public SSH targets does not apply.
- Requiring `known_hosts` management would add setup friction (the SSH key must first be added to the file, or the user must manually verify and accept the host fingerprint on first connection). Pueo's `setup.sh` already handles SSH key generation and distribution; adding host key pinning would require an additional step with no commensurate security benefit in the intended deployment environment.
- If the target host changes (e.g., HA migration), a stale `known_hosts` entry would cause connection failures that are harder to diagnose than a MITM warning.

## Consequences
- Every security review must note the `known_hosts=None` setting and confirm it is intentional. CLAUDE.md Key Patterns documents this explicitly.
- If Pueo is ever deployed to connect to hosts over a public network or untrusted LAN, host key verification must be enabled before that deployment.
- CLAUDE.md instructs reviewers to flag this in security reviews — it is a known accepted risk, not an oversight.

## Related decisions
- [ADR 001 — Config centralization](001-config-centralization.md): `HA_HOST`, `HA_USER`, and `SSH_KEY_PATH` are the single source of SSH connection parameters.
