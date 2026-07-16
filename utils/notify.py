"""HITL notification infrastructure.

Three concrete notifiers:
  FileNotifier   — writes a JSON file; agent polls for .approved/.rejected sibling
  NtfyNotifier   — HTTP POST to ntfy.sh or a self-hosted instance
  WebhookNotifier — generic HTTP POST for HA automations or other webhooks

FakeNotifier is for tests: captures sent notifications and lets callers
pre-configure the approval outcome without touching the filesystem.
"""

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Protocol


class NotifierProtocol(Protocol):
    async def send(self, subject: str, body: str, payload: dict) -> None: ...

    async def wait_for_approval(self, notification_id: str) -> bool: ...


class HITLRejected(Exception):
    """Raised when a human explicitly rejects a pending repair action."""


class FileNotifier:
    """Writes a JSON file to a watch directory and polls for .approved / .rejected."""

    def __init__(self, watch_dir: str, poll_interval: float = 5.0) -> None:
        self._dir = Path(watch_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._poll = poll_interval

    async def send(self, subject: str, body: str, payload: dict) -> None:
        nid = payload.get("notification_id", str(uuid.uuid4()))
        record = {
            "notification_id": nid,
            "subject": subject,
            "body": body,
            "payload": payload,
            "sent_at": int(time.time()),
        }
        (self._dir / f"{nid}.json").write_text(json.dumps(record, indent=2))

    async def wait_for_approval(self, notification_id: str) -> bool:
        approved_path = self._dir / f"{notification_id}.approved"
        rejected_path = self._dir / f"{notification_id}.rejected"
        while True:
            if approved_path.exists():
                return True
            if rejected_path.exists():
                return False
            await asyncio.sleep(self._poll)


class NtfyNotifier:
    """HTTP POST to ntfy.sh (or a self-hosted ntfy server).

    Approval is file-based: after the push notification is sent the caller
    writes a ``<notification_id>.approved`` or ``<notification_id>.rejected``
    file to ``watch_dir`` and this notifier polls for it — the same mechanism
    used by FileNotifier.  ntfy.sh is one-way push and cannot relay a reply
    back to the agent directly.
    """

    def __init__(self, url: str, watch_dir: str = "hitl/") -> None:
        self._url = url
        self._file_notifier = FileNotifier(watch_dir=watch_dir)

    async def send(self, subject: str, body: str, payload: dict) -> None:
        import urllib.request

        data = json.dumps({"title": subject, "message": body, **payload}).encode()
        req = urllib.request.Request(
            self._url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        await asyncio.to_thread(urllib.request.urlopen, req)

    async def wait_for_approval(self, notification_id: str) -> bool:
        return await self._file_notifier.wait_for_approval(notification_id)


class WebhookNotifier:
    """Generic HTTP POST for HA automations or other webhook consumers."""

    def __init__(self, url: str) -> None:
        self._url = url

    async def send(self, subject: str, body: str, payload: dict) -> None:
        import urllib.request

        data = json.dumps({"subject": subject, "body": body, **payload}).encode()
        req = urllib.request.Request(
            self._url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        await asyncio.to_thread(urllib.request.urlopen, req)

    async def wait_for_approval(self, notification_id: str) -> bool:
        raise NotImplementedError(
            "WebhookNotifier requires an out-of-band approval mechanism "
            "(e.g., pair with FileNotifier for polling)"
        )


class FakeNotifier:
    """Test double: records calls and returns a pre-configured approval outcome."""

    def __init__(self, approve: bool = True) -> None:
        self.approve = approve
        self.sent: list[dict] = []

    async def send(self, subject: str, body: str, payload: dict) -> None:
        self.sent.append({"subject": subject, "body": body, "payload": payload})

    async def wait_for_approval(self, notification_id: str) -> bool:
        return self.approve


def get_notifier(
    notifier_type: str,
    notify_url: str = "",
    notify_watch_dir: str = "hitl/",
) -> "NotifierProtocol":
    if notifier_type == "ntfy":
        return NtfyNotifier(url=notify_url, watch_dir=notify_watch_dir)
    if notifier_type == "webhook":
        return WebhookNotifier(url=notify_url)
    return FileNotifier(watch_dir=notify_watch_dir)
