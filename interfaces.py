"""Protocol interfaces for SSH, LLM, and HA REST clients."""

from typing import Any, AsyncIterator, Protocol


class SSHClientProtocol(Protocol):
    async def read_file(self, path: str) -> str: ...

    async def write_file(self, path: str, content: str) -> None: ...

    async def download_file(self, remote_path: str, local_path: str) -> None: ...

    async def run(self, command: str, check: bool = False) -> tuple[int, str, str]: ...

    def stream_lines(self, command: str) -> AsyncIterator[str]: ...


class LLMClientProtocol(Protocol):
    async def chat(
        self,
        model: str,
        messages: list[dict],
        options: dict,
        format: dict,
    ) -> Any: ...


class HARestClientProtocol(Protocol):
    async def get_states(self, prefix: str | None = None) -> list[dict]: ...

    async def get_state(self, entity_id: str) -> dict: ...

    async def call_service(self, domain: str, service: str, payload: dict) -> dict: ...
