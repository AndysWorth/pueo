"""Protocol interfaces for SSH, LLM, HA REST, and knowledge store clients."""

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

    async def chat_with_tools(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict],
        options: dict | None = None,
    ) -> dict: ...


class HARestClientProtocol(Protocol):
    async def get_states(self, prefix: str | None = None) -> list[dict]: ...

    async def get_state(self, entity_id: str) -> dict: ...

    async def call_service(self, domain: str, service: str, payload: dict) -> dict: ...


class HAWebSocketClientProtocol(Protocol):
    async def get_device_registry(self) -> list[dict]: ...


class NetAlertXClientProtocol(Protocol):
    async def get_devices(self) -> list[dict]: ...


class KnowledgeStoreClientProtocol(Protocol):
    def upsert(
        self,
        collection: str,
        ids: list[str],
        documents: list[str],
        metadatas: list[dict],
    ) -> None: ...

    def query(
        self,
        query_text: str,
        top_k: int,
        collections: list[str] | None = None,
    ) -> list[Any]: ...
