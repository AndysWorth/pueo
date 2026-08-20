"""HA REST API client, update entity polling, and FakeHARestClient test double."""

from dataclasses import dataclass, field
from typing import Optional

import httpx

from interfaces import HARestClientProtocol, HAWebSocketClientProtocol


@dataclass
class UpdateStatus:
    component: str
    entity_id: str
    installed_version: str
    latest_version: str
    update_available: bool
    release_url: Optional[str]
    release_summary: Optional[str]
    in_progress: bool


_COMPONENT_MAP = {
    "home_assistant_core": "core",
    "home_assistant_os": "os",
    "home_assistant_operating_system": "os",  # actual entity name on modern HAOS
    "home_assistant_supervisor": "supervisor",
}


def _entity_to_update_status(entity: dict) -> UpdateStatus:
    entity_id = entity["entity_id"]
    attrs = entity.get("attributes", {})
    name = entity_id.removeprefix("update.")
    if name.endswith("_update"):
        name = name[: -len("_update")]
    component = _COMPONENT_MAP.get(name, name)
    return UpdateStatus(
        component=component,
        entity_id=entity_id,
        installed_version=attrs.get("installed_version", ""),
        latest_version=attrs.get("latest_version", ""),
        update_available=(entity.get("state", "off") == "on"),
        release_url=attrs.get("release_url"),
        release_summary=attrs.get("release_summary"),
        in_progress=bool(attrs.get("in_progress", False)),
    )


async def get_update_status(
    ha_rest_client: HARestClientProtocol,
) -> list[UpdateStatus]:
    """Return UpdateStatus for every update.* entity visible via the HA REST API."""
    entities = await ha_rest_client.get_states(prefix="update.")
    return [_entity_to_update_status(e) for e in entities]


class HARestClient:  # pragma: no cover
    """Real HA REST API client using httpx."""

    def __init__(self, host: str, port: int, token: str, timeout: float = 10.0) -> None:
        self._base_url = f"http://{host}:{port}/api"
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        self._timeout = timeout

    async def get_states(self, prefix: str | None = None) -> list[dict]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(
                f"{self._base_url}/states",
                headers=self._headers,
            )
            resp.raise_for_status()
            states: list[dict] = resp.json()
        if prefix:
            return [s for s in states if s.get("entity_id", "").startswith(prefix)]
        return states

    async def get_state(self, entity_id: str) -> dict:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(
                f"{self._base_url}/states/{entity_id}",
                headers=self._headers,
            )
            resp.raise_for_status()
            return resp.json()  # type: ignore[no-any-return]

    async def call_service(self, domain: str, service: str, payload: dict) -> dict:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                f"{self._base_url}/services/{domain}/{service}",
                headers=self._headers,
                json=payload,
            )
            resp.raise_for_status()
            return resp.json()  # type: ignore[no-any-return]

    async def get_raw(self, path: str) -> dict:
        base = self._base_url.removesuffix("/api")
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(f"{base}{path}", headers=self._headers)
            resp.raise_for_status()
            return resp.json()  # type: ignore[no-any-return]

    async def post(self, path: str, payload: dict) -> dict:
        base = self._base_url.removesuffix("/api")
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                f"{base}{path}", headers=self._headers, json=payload
            )
            resp.raise_for_status()
            return resp.json()  # type: ignore[no-any-return]

    async def delete(self, path: str) -> None:
        base = self._base_url.removesuffix("/api")
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.delete(f"{base}{path}", headers=self._headers)
            resp.raise_for_status()


@dataclass
class HARepairIssue:
    domain: str
    issue_id: str
    severity: str
    issue_key: str
    breaks_in_ha_version: Optional[str]
    translation_key: Optional[str] = None
    data: dict = field(default_factory=dict)


async def get_ha_repair_issues(
    ws_client: HAWebSocketClientProtocol,
) -> list[HARepairIssue]:
    """Fetch active repair issues via the HA WebSocket API (repairs/list_issues).

    The REST endpoint /api/repairs/issues does not exist on all HA versions;
    WebSocket is the canonical source for repair data.
    """
    result: list[HARepairIssue] = []
    items = await ws_client.get_repair_issues()
    for item in items:
        domain = item.get("domain", "")
        issue_id = item.get("issue_id", "")
        if not domain or not issue_id:
            continue
        if item.get("ignored"):
            continue
        result.append(
            HARepairIssue(
                domain=domain,
                issue_id=issue_id,
                severity=item.get("severity", "warning"),
                issue_key=f"{domain}/{issue_id}",
                breaks_in_ha_version=item.get("breaks_in_ha_version"),
                translation_key=item.get("translation_key"),
                data=item,
            )
        )
    return result


async def dismiss_ha_repair_issue(
    rest: HARestClientProtocol,
    domain: str,
    issue_id: str,
) -> None:
    """DELETE /api/repairs/issues/{domain}/{issue_id} — dismiss the repair."""
    await rest.delete(f"/api/repairs/issues/{domain}/{issue_id}")


class FakeHARestClient:
    """Test double for HARestClientProtocol."""

    def __init__(
        self,
        states: list[dict] | None = None,
        raw_responses: dict[str, dict] | None = None,
    ) -> None:
        self._states: list[dict] = states or []
        self._raw: dict[str, dict] = raw_responses or {}
        self.service_calls: list[tuple[str, str, dict]] = []
        self.deleted: list[str] = []
        self.posted: list[tuple[str, dict]] = []

    async def get_states(self, prefix: str | None = None) -> list[dict]:
        if prefix:
            return [
                s for s in self._states if s.get("entity_id", "").startswith(prefix)
            ]
        return list(self._states)

    async def get_state(self, entity_id: str) -> dict:
        for s in self._states:
            if s.get("entity_id") == entity_id:
                return s
        raise httpx.HTTPStatusError(
            message=f"404 Not Found: {entity_id}",
            request=httpx.Request("GET", f"http://fake/api/states/{entity_id}"),
            response=httpx.Response(404),
        )

    async def call_service(self, domain: str, service: str, payload: dict) -> dict:
        self.service_calls.append((domain, service, payload))
        return {}

    async def get_raw(self, path: str) -> dict:
        return self._raw.get(path, {})

    async def post(self, path: str, payload: dict) -> dict:
        self.posted.append((path, payload))
        return {}

    async def delete(self, path: str) -> None:
        self.deleted.append(path)
