"""Protocol interfaces for SSH and LLM clients."""

from typing import Any, AsyncIterator, Protocol


class SSHClientProtocol(Protocol):
    async def read_file(self, path: str) -> str: ...

    async def write_file(self, path: str, content: str) -> None: ...

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
