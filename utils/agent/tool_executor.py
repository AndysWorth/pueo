"""Tool executor — dispatches tool calls to their implementations (item 43).

ToolExecutor is initialized with SSH clients, autonomy gate, and notifier.
Each tool method returns a ToolResult; errors are captured rather than raised.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import subprocess  # nosec B404 — commands are fixed CI tools (black, flake8, mypy, pytest), no user input
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

import config as _config_mod
from config import CHAT_MEMORY_TOP_K, CONFIG_REMOTE_PATH, DB_PATH
from utils.core.logging import get_correlation_id, get_logger
from utils.agent.tool_registry import ToolCall, ToolResult

if TYPE_CHECKING:
    from interfaces import (
        HAWebSocketClientProtocol,
        KnowledgeStoreClientProtocol,
        LLMClientProtocol,
        SSHClientProtocol,
    )
    from netalertx.api_client import NetAlertXAPIClient
    from utils.agent.autonomy import AutonomyGate
    from utils.ha.ha_environment import HAEnvironmentProfile
    from utils.notify import NotifierProtocol
    from utils.agent.tool_registry import FixEnrichment

log = get_logger("tool_executor")

_REPO_ROOT = Path(__file__).parent.parent
_SOURCE_ALLOWED_EXTENSIONS: frozenset[str] = frozenset(
    {".py", ".yaml", ".md", ".toml", ".txt"}
)

_HA_COMMAND_ALLOWLIST: frozenset[str] = frozenset(
    {
        "ha core check",
        "ha core restart",
        "ha core stop",
        "ha host info",
        "ha backups list",
        "ha apps list",
        "ha os info",
    }
)

_READ_FILE_ALLOWED_PREFIXES: tuple[str, ...] = ("/config/", "/backup/")

_HA_SOURCE_RAW_URL = (
    "https://raw.githubusercontent.com/home-assistant/core/dev"
    "/homeassistant/components/{domain}/{filename}"
)
_MAX_HA_DOC_FETCH_CHARS: int = 16_000
_MAX_FETCH_URL_CHARS: int = 8_000
_PRIVATE_IP_BLOCKS: tuple[str, ...] = (
    "10.",
    "172.16.",
    "172.17.",
    "172.18.",
    "172.19.",
    "172.20.",
    "172.21.",
    "172.22.",
    "172.23.",
    "172.24.",
    "172.25.",
    "172.26.",
    "172.27.",
    "172.28.",
    "172.29.",
    "172.30.",
    "172.31.",
    "192.168.",
    "127.",
    "169.254.",
    "::1",
    "fc",
    "fd",
)

# Files the agent may never patch autonomously — manual edit + security review required.
_SAFETY_CRITICAL_PATHS: frozenset[str] = frozenset(
    {
        "utils/autonomy.py",
        "interfaces.py",
        "config.py",
    }
)

# Function definitions that belong exclusively to the backup invariant chain.
# A patch that introduces one of these definitions would bypass or duplicate
# the invariant, so it is blocked regardless of target file.
_BACKUP_INVARIANT_SYMBOLS: tuple[str, ...] = (
    "def execute_remote_backup",
    "def record_backup_slug",
)


class ToolExecutor:
    """Executes tool calls on behalf of AgentLoop.

    ha_ssh_client and nax_ssh_client may be the same object when NetAlertX
    runs as a supervisor add-on on the same HA host.
    """

    def __init__(
        self,
        ha_ssh_client: "SSHClientProtocol",
        gate: "AutonomyGate",
        notifier: "NotifierProtocol",
        nax_ssh_client: Optional["SSHClientProtocol"] = None,
        netalertx_api_client: Optional["NetAlertXAPIClient"] = None,
        netalertx_container_name: str = "netalertx",
        ha_ws_client: Optional["HAWebSocketClientProtocol"] = None,
        knowledge_store: Optional["KnowledgeStoreClientProtocol"] = None,
        db_path: str = DB_PATH,
        llm_client: Optional["LLMClientProtocol"] = None,
    ) -> None:
        self._ha_ssh = ha_ssh_client
        self._nax_ssh = nax_ssh_client
        self._gate = gate
        self._notifier = notifier
        self._api = netalertx_api_client
        self._container = netalertx_container_name
        self._ws_client = ha_ws_client
        self._knowledge_store = knowledge_store
        self._db_path = db_path
        self._llm_client = llm_client
        self._apply_fix_used = False
        self._pending_patch: dict[str, str] = {}
        self._sandbox_passed: bool = False
        self._sandbox_output: str = ""
        self._dynamic_tools: dict[str, Callable[..., Any]] = {}
        self._ha_profile: Optional["HAEnvironmentProfile"] = None

    def reset(self) -> None:
        """Reset per-loop state. Called by AgentLoop before each run()."""
        self._apply_fix_used = False
        self._pending_patch = {}
        self._sandbox_passed = False
        self._sandbox_output = ""
        # _dynamic_tools intentionally not reset — registered tools persist across loops

    def register_dynamic_tool(self, name: str, fn: "Callable[..., Any]") -> None:
        """Register a user-approved dynamic tool callable by name."""
        self._dynamic_tools[name] = fn

    def set_ha_profile(self, profile: "HAEnvironmentProfile") -> None:
        """Cache the HA environment profile so get_ha_profile tool can return it."""
        self._ha_profile = profile

    def set_ws_client(self, client: "HAWebSocketClientProtocol") -> None:
        """Inject the HA WebSocket client after construction (HA_API_TOKEN may not be
        available at executor creation time). Called from main.py once the client exists.
        """
        self._ws_client = client

    async def execute(self, tool_call: ToolCall) -> ToolResult:
        args = tool_call.arguments
        name = tool_call.name
        log.info("tool_execute", tool=name)
        try:
            if name == "read_config":
                return await self._read_config(args.get("path", CONFIG_REMOTE_PATH))
            if name == "read_logs":
                return await self._read_logs(int(args.get("lines", 100)))
            if name == "run_ha_command":
                return await self._run_ha_command(args.get("command", ""))
            if name == "trigger_backup":
                return await self._trigger_backup()
            if name == "read_file":
                return await self._read_file(args.get("path", ""))
            if name == "query_netalertx":
                return await self._query_netalertx(args.get("query_type", "health"))
            if name == "apply_fix":
                return await self._apply_fix(
                    args.get("yaml_content", ""),
                    args.get("description", ""),
                )
            if name == "verify_fix":
                return await self._verify_fix()
            if name == "finish_repair":
                return ToolResult(
                    tool_name="finish_repair",
                    success=True,
                    output=args.get("summary", "Repair complete"),
                )
            if name == "finish_chat":
                return ToolResult(
                    tool_name="finish_chat",
                    success=True,
                    output=args.get("summary", ""),
                )
            if name == "finish_investigation":
                return ToolResult(
                    tool_name="finish_investigation",
                    success=True,
                    output=args.get("summary", "Investigation complete"),
                )
            if name == "query_knowledge":
                return await self._query_knowledge(
                    args.get("query", ""),
                    integration_filter=args.get("integration_filter"),
                )
            if name == "remember":
                return await self._remember(
                    args.get("key", ""),
                    args.get("content", ""),
                    args.get("source", "agent"),
                )
            if name == "recall":
                return await self._recall(args.get("query", ""))
            if name == "get_ha_profile":
                return await self._get_ha_profile()
            if name == "get_disk_usage":
                return await self._get_disk_usage()
            if name == "fetch_ha_docs":
                return await self._fetch_ha_docs(
                    args.get("domain", ""), args.get("filename", "")
                )
            if name == "fetch_url":
                return await self._fetch_url(args.get("url", ""))
            if name == "read_source":
                return await self._read_source(args.get("path", ""))
            if name == "propose_patch":
                return await self._propose_patch(
                    args.get("path", ""), args.get("content", "")
                )
            if name == "sandbox_code":
                return await self._sandbox_code(args.get("description", ""))
            if name == "add_tool":
                return await self._add_tool(
                    args.get("name", ""),
                    args.get("description", ""),
                    args.get("parameters_schema", ""),
                    args.get("code", ""),
                )
            if name == "open_pr":
                return await self._open_pr(
                    args.get("title", ""),
                    args.get("reason", ""),
                    args.get("branch_name"),
                )
            if name == "investigate_device":
                return await self._investigate_device(args.get("ip", ""))
            if name == "switch_model":
                return await self._switch_model(args.get("model_name"))
            if name == "restart_netalertx":
                return await self._restart_netalertx()
            if name == "rewrite_netalertx_conf":
                return await self._rewrite_netalertx_conf(args.get("overrides", {}))
            if name in self._dynamic_tools:
                result = await self._dynamic_tools[name](args)
                if isinstance(result, ToolResult):
                    return result
                return ToolResult(tool_name=name, success=True, output=str(result))
            return ToolResult(
                tool_name=name,
                success=False,
                output="",
                error=f"Unknown tool: {name!r}",
            )
        except Exception as exc:
            log.error("tool_execute_error", tool=name, error=str(exc))
            return ToolResult(tool_name=name, success=False, output="", error=str(exc))

    # ------------------------------------------------------------------
    # HA tools
    # ------------------------------------------------------------------

    async def _read_config(self, path: str) -> ToolResult:
        try:
            content = await self._ha_ssh.read_file(path)
            return ToolResult(tool_name="read_config", success=True, output=content)
        except Exception as exc:
            return ToolResult(
                tool_name="read_config", success=False, output="", error=str(exc)
            )

    async def _read_logs(self, lines: int) -> ToolResult:
        try:
            _, stdout, stderr = await self._ha_ssh.run(
                f"ha core logs --lines {lines}", check=False
            )
            return ToolResult(
                tool_name="read_logs", success=True, output=stdout or stderr
            )
        except Exception as exc:
            return ToolResult(
                tool_name="read_logs", success=False, output="", error=str(exc)
            )

    async def _run_ha_command(self, command: str) -> ToolResult:
        normalized = command.strip()
        if normalized not in _HA_COMMAND_ALLOWLIST:
            log.warning("run_ha_command_rejected", command=normalized)
            return ToolResult(
                tool_name="run_ha_command",
                success=False,
                output="",
                error=f"Command not in allowlist: {normalized!r}",
            )
        try:
            exit_code, stdout, stderr = await self._ha_ssh.run(normalized, check=False)
            return ToolResult(
                tool_name="run_ha_command",
                success=exit_code == 0,
                output=stdout or stderr,
            )
        except Exception as exc:
            return ToolResult(
                tool_name="run_ha_command", success=False, output="", error=str(exc)
            )

    async def _trigger_backup(self) -> ToolResult:
        from agents.ha_agent_advanced import (
            enforce_ha_retention,
            execute_remote_backup,
            offload_backup_to_local,
            purge_local_backups,
            record_backup_slug,
        )

        try:
            slug = await execute_remote_backup(ssh_client=self._ha_ssh)
        except Exception as exc:
            return ToolResult(
                tool_name="trigger_backup", success=False, output="", error=str(exc)
            )
        record_backup_slug(slug)
        offloaded = await offload_backup_to_local(slug, ssh_client=self._ha_ssh)
        try:
            await enforce_ha_retention(ssh_client=self._ha_ssh)
            purge_local_backups()
        except Exception:  # nosec B110 — retention errors don't invalidate the backup
            pass
        return ToolResult(
            tool_name="trigger_backup",
            success=True,
            output=f"Backup created slug={slug}, offloaded={offloaded}",
        )

    async def _read_file(self, path: str) -> ToolResult:
        if not any(path.startswith(p) for p in _READ_FILE_ALLOWED_PREFIXES):
            log.warning("read_file_rejected", path=path)
            return ToolResult(
                tool_name="read_file",
                success=False,
                output="",
                error=f"Path not in allowed directories: {path!r}",
            )
        try:
            content = await self._ha_ssh.read_file(path)
            return ToolResult(tool_name="read_file", success=True, output=content)
        except Exception as exc:
            return ToolResult(
                tool_name="read_file", success=False, output="", error=str(exc)
            )

    async def _query_knowledge(
        self,
        query: str,
        integration_filter: list[str] | None = None,
    ) -> ToolResult:
        if self._knowledge_store is None:
            return ToolResult(
                tool_name="query_knowledge",
                success=False,
                output="",
                error="Knowledge store not configured (run --mode rag-refresh first)",
            )
        from config import RAG_TOP_K

        where = None
        if integration_filter:
            where = {"impacted_integration": {"$in": integration_filter}}
        chunks = self._knowledge_store.query(query, top_k=RAG_TOP_K, where=where)
        if not chunks:
            return ToolResult(
                tool_name="query_knowledge",
                success=True,
                output="No relevant knowledge found.",
            )
        output = "\n\n".join(f"[{c.collection} | {c.source}]\n{c.text}" for c in chunks)
        return ToolResult(tool_name="query_knowledge", success=True, output=output)

    # ------------------------------------------------------------------
    # Conversational memory tools
    # ------------------------------------------------------------------

    async def _remember(self, key: str, content: str, source: str) -> ToolResult:
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute(
                    "INSERT INTO agent_memory (key, content, source, ts) VALUES (?, ?, ?, ?)",
                    (key, content, source, time.time()),
                )
            return ToolResult(
                tool_name="remember", success=True, output=f"Remembered: {key}"
            )
        except Exception as exc:
            return ToolResult(
                tool_name="remember", success=False, output="", error=str(exc)
            )

    async def _recall(self, query: str) -> ToolResult:
        try:
            with sqlite3.connect(self._db_path) as conn:
                rows = conn.execute(
                    "SELECT key, content, source, ts FROM agent_memory "
                    "WHERE content LIKE ? OR key LIKE ? "
                    "ORDER BY ts DESC LIMIT ?",
                    (f"%{query}%", f"%{query}%", CHAT_MEMORY_TOP_K),
                ).fetchall()
            if not rows:
                return ToolResult(
                    tool_name="recall", success=True, output="Nothing found."
                )
            lines = [f"[{r[0]}] ({r[2]}) {r[1]}" for r in rows]
            return ToolResult(tool_name="recall", success=True, output="\n".join(lines))
        except Exception as exc:
            return ToolResult(
                tool_name="recall", success=False, output="", error=str(exc)
            )

    async def _get_ha_profile(self) -> ToolResult:
        if self._ha_profile is None:
            return ToolResult(
                tool_name="get_ha_profile",
                success=True,
                output=(
                    "HA environment profile not yet available. "
                    "Restart the supervisor to build it."
                ),
            )
        import dataclasses

        return ToolResult(
            tool_name="get_ha_profile",
            success=True,
            output=json.dumps(dataclasses.asdict(self._ha_profile), indent=2),
        )

    async def _get_disk_usage(self) -> ToolResult:
        """Return disk breakdown — use cached if fresh enough, else fetch via SSH."""
        import time

        from utils.disk_usage import fetch_disk_breakdown, get_disk_breakdown

        cached = get_disk_breakdown()
        age = time.time() - (cached.fetched_at if cached is not None else 0)
        if cached is not None and age < 300:
            breakdown = cached
        else:
            breakdown = await fetch_disk_breakdown(self._ha_ssh)

        lines = [
            f"Disk: {breakdown.disk_used_gb:.1f} GB used / "
            f"{breakdown.disk_total_gb:.1f} GB total "
            f"({breakdown.disk_used_pct:.1f}% full, "
            f"{breakdown.disk_free_gb:.1f} GB free)",
            "",
        ]
        for section in breakdown.sections:
            if section.items:
                lines.append(f"{section.title}: {section.total_human}")
                for item in section.items[:10]:
                    lines.append(
                        f"  {item.name}: {item.size_human} ({item.pct_of_section:.0f}%)"
                    )
                lines.append("")
        if breakdown.container_images_estimated_gb is not None:
            lines.append(
                f"OS + container images: ~{breakdown.container_images_estimated_gb:.1f} GB"
                " (estimated)"
            )
        return ToolResult(
            tool_name="get_disk_usage",
            success=True,
            output="\n".join(lines),
        )

    # ------------------------------------------------------------------
    # Code sandbox tools (item 70)
    # ------------------------------------------------------------------

    def _resolve_repo_path(self, path: str) -> "Path | str":
        """Resolve a repo-relative path. Returns Path on success, error string on failure."""
        try:
            resolved = (_REPO_ROOT / path).resolve()
        except Exception as exc:
            return str(exc)
        if not resolved.is_relative_to(_REPO_ROOT.resolve()):
            return f"Path traversal rejected: {path!r}"
        if resolved.suffix not in _SOURCE_ALLOWED_EXTENSIONS:
            allowed = sorted(_SOURCE_ALLOWED_EXTENSIONS)
            return f"Extension not allowed: {resolved.suffix!r} (allowed: {allowed})"
        return resolved

    async def _read_source(self, path: str) -> ToolResult:
        result = self._resolve_repo_path(path)
        if isinstance(result, str):
            return ToolResult(
                tool_name="read_source", success=False, output="", error=result
            )
        try:
            content = result.read_text()
            if len(content) > 8000:
                content = content[:8000]
            return ToolResult(tool_name="read_source", success=True, output=content)
        except Exception as exc:
            return ToolResult(
                tool_name="read_source", success=False, output="", error=str(exc)
            )

    async def _fetch_ha_docs(self, domain: str, filename: str) -> ToolResult:
        """Return HA component source from cache; fetch live only in cloud/both mode."""
        # Path-traversal guard on domain and filename
        if "/" in domain or ".." in domain or "/" in filename or ".." in filename:
            return ToolResult(
                tool_name="fetch_ha_docs",
                success=False,
                output="",
                error="Path traversal rejected",
            )

        cache_dir = Path(
            getattr(_config_mod, "HA_SOURCE_CACHE_DIR", ".cache/ha_source/")
        )
        cache_path = cache_dir / domain / filename

        if cache_path.exists():
            try:
                content = cache_path.read_text(encoding="utf-8")
                return ToolResult(
                    tool_name="fetch_ha_docs", success=True, output=content
                )
            except OSError as exc:
                return ToolResult(
                    tool_name="fetch_ha_docs",
                    success=False,
                    output="",
                    error=str(exc),
                )

        provider = getattr(_config_mod, "LLM_PROVIDER", "local")
        if provider == "local":
            return ToolResult(
                tool_name="fetch_ha_docs",
                success=False,
                output="",
                error=(
                    f"Cache miss for {domain}/{filename} and LLM_PROVIDER=local "
                    "(no WAN allowed). Run rag-refresh to pre-populate, "
                    "or use query_knowledge for available docs."
                ),
            )

        # cloud or both — fetch live
        import urllib.request

        url = _HA_SOURCE_RAW_URL.format(domain=domain, filename=filename)
        req = urllib.request.Request(url, headers={"User-Agent": "pueo-ha-lookup/1.0"})
        try:
            raw = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: urllib.request.urlopen(req, timeout=60).read(),  # nosec B310
            )
        except Exception as exc:
            return ToolResult(
                tool_name="fetch_ha_docs",
                success=False,
                output="",
                error=f"Fetch failed for {domain}/{filename}: {exc}",
            )

        text = raw.decode("utf-8", errors="replace")[:_MAX_HA_DOC_FETCH_CHARS]
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            cache_path.write_text(text, encoding="utf-8")
        except OSError:
            pass  # cache write failure is non-fatal
        return ToolResult(tool_name="fetch_ha_docs", success=True, output=text)

    async def _fetch_url(self, url: str) -> ToolResult:
        """GET an external URL for diagnostic verification (read-only, private IPs blocked)."""
        import urllib.parse
        import urllib.request

        allow_wan = getattr(_config_mod, "ALLOW_DIAGNOSTIC_WAN", True)
        if not allow_wan:
            return ToolResult(
                tool_name="fetch_url",
                success=False,
                output="",
                error="ALLOW_DIAGNOSTIC_WAN is disabled in config.",
            )

        if not url.startswith(("http://", "https://")):
            return ToolResult(
                tool_name="fetch_url",
                success=False,
                output="",
                error="Only http:// and https:// URLs are allowed.",
            )

        parsed = urllib.parse.urlparse(url)
        hostname = parsed.hostname or ""
        if any(
            hostname.startswith(block) for block in _PRIVATE_IP_BLOCKS
        ) or hostname in (
            "localhost",
            "homeassistant.local",
        ):
            return ToolResult(
                tool_name="fetch_url",
                success=False,
                output="",
                error=f"Blocked: {hostname!r} resolves to a private/loopback address.",
            )

        timeout = int(getattr(_config_mod, "DIAGNOSTIC_WAN_TIMEOUT_SECONDS", 60))
        req = urllib.request.Request(url, headers={"User-Agent": "pueo-diagnostic/1.0"})
        try:
            raw = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: urllib.request.urlopen(
                    req, timeout=timeout
                ).read(),  # nosec B310
            )
        except Exception as exc:
            return ToolResult(
                tool_name="fetch_url",
                success=False,
                output="",
                error=str(exc),
            )

        text = raw.decode("utf-8", errors="replace")[:_MAX_FETCH_URL_CHARS]
        return ToolResult(tool_name="fetch_url", success=True, output=text)

    async def _propose_patch(self, path: str, content: str) -> ToolResult:
        result = self._resolve_repo_path(path)
        if isinstance(result, str):
            return ToolResult(
                tool_name="propose_patch", success=False, output="", error=result
            )
        # Safety-critical file block list (item 85): derive canonical relative path
        # so that path aliases ("./config.py", "config.py") all hit the same check.
        try:
            rel = str(result.relative_to(_REPO_ROOT.resolve()))
        except ValueError:
            rel = path
        if rel in _SAFETY_CRITICAL_PATHS:
            return ToolResult(
                tool_name="propose_patch",
                success=False,
                output="",
                error=(
                    f"Patching {rel!r} is blocked — safety-critical file. "
                    "Manual edit and security review required."
                ),
            )
        # Backup invariant chain protection (item 85): block patches that introduce
        # a new definition of backup-invariant functions — calling them is fine;
        # redefining them could silently bypass the invariant.
        for symbol in _BACKUP_INVARIANT_SYMBOLS:
            if symbol in content:
                return ToolResult(
                    tool_name="propose_patch",
                    success=False,
                    output="",
                    error=(
                        f"Patch redefines {symbol!r} — backup invariant chain is "
                        "protected. Manual edit and security review required."
                    ),
                )
        self._pending_patch[path] = content
        self._sandbox_passed = False
        self._sandbox_output = ""
        return ToolResult(
            tool_name="propose_patch",
            success=True,
            output=f"Patch staged for {path!r}. Call sandbox_code to validate.",
        )

    async def _sandbox_code(self, description: str) -> ToolResult:
        if not self._pending_patch:
            return ToolResult(
                tool_name="sandbox_code",
                success=False,
                output="",
                error="No pending patch. Call propose_patch first.",
            )
        tmpdir = Path(tempfile.mkdtemp(prefix="pueo_sandbox_"))
        try:
            shutil.copytree(
                str(_REPO_ROOT),
                str(tmpdir),
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(
                    ".venv", "__pycache__", ".git", "*.pyc", "chromadb_data"
                ),
            )
            for rel_path, file_content in self._pending_patch.items():
                target = tmpdir / rel_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(file_content)

            combined: list[str] = []
            all_passed = True

            for rel_path in self._pending_patch:
                patched_file = str(tmpdir / rel_path)
                for cmd in [
                    [sys.executable, "-m", "black", "--check", patched_file],
                    [
                        sys.executable,
                        "-m",
                        "flake8",
                        "--count",
                        "--select=E9,F63,F7,F82",
                        "--show-source",
                        "--statistics",
                        patched_file,
                    ],
                    [
                        sys.executable,
                        "-m",
                        "mypy",
                        "--ignore-missing-imports",
                        patched_file,
                    ],
                ]:
                    label = " ".join(cmd[2:])
                    try:
                        proc = await asyncio.wait_for(
                            asyncio.to_thread(
                                subprocess.run,
                                cmd,
                                capture_output=True,
                                text=True,
                                timeout=60,
                                cwd=str(tmpdir),
                            ),
                            timeout=65,
                        )
                        out = (proc.stdout + proc.stderr).strip()
                        if out:
                            combined.append(f"$ {label}\n{out}")
                        if proc.returncode != 0:
                            all_passed = False
                    except asyncio.TimeoutError:
                        combined.append(f"$ {label}\nTimeout after 60s")
                        all_passed = False

            pytest_cmd = [
                sys.executable,
                "-m",
                "pytest",
                "tests/",
                "--tb=short",
                "--ignore=tests/integration",
                "-x",
                "-q",
            ]
            try:
                proc = await asyncio.wait_for(
                    asyncio.to_thread(
                        subprocess.run,
                        pytest_cmd,
                        capture_output=True,
                        text=True,
                        timeout=60,
                        cwd=str(tmpdir),
                    ),
                    timeout=65,
                )
                out = (proc.stdout + proc.stderr).strip()
                if out:
                    combined.append(f"$ pytest tests/\n{out}")
                if proc.returncode != 0:
                    all_passed = False
            except asyncio.TimeoutError:
                combined.append("$ pytest tests/\nTimeout after 60s")
                all_passed = False

            full_output = "\n\n".join(combined)
            if len(full_output) > 3000:
                full_output = full_output[:3000]

            self._sandbox_passed = all_passed
            self._sandbox_output = full_output
            return ToolResult(
                tool_name="sandbox_code", success=all_passed, output=full_output
            )
        finally:
            shutil.rmtree(str(tmpdir), ignore_errors=True)

    async def _add_tool(
        self, name: str, description: str, parameters_schema: str, code: str
    ) -> ToolResult:
        from config import CHAT_ALLOW_TOOL_REGISTRATION

        if not CHAT_ALLOW_TOOL_REGISTRATION:
            return ToolResult(
                tool_name="add_tool",
                success=False,
                output="",
                error="CHAT_ALLOW_TOOL_REGISTRATION is disabled; enable it in config to register tools",
            )
        try:
            compile(code, "<string>", "exec")
        except SyntaxError as exc:
            return ToolResult(
                tool_name="add_tool",
                success=False,
                output="",
                error=f"Syntax error in proposed code: {exc}",
            )
        if not self._sandbox_passed:
            return ToolResult(
                tool_name="add_tool",
                success=False,
                output="",
                error="Run sandbox_code first and ensure all CI checks pass",
            )
        from utils.card_types import CARD_TYPE_CODE_PROPOSAL

        nid = get_correlation_id() or str(uuid.uuid4())
        await self._notifier.send(
            subject=f"Pueo: add_tool approval required — {name}",
            body=f"Register new tool: {name}\n\n{description}",
            payload={
                "notification_id": nid,
                "card_type": CARD_TYPE_CODE_PROPOSAL,
                "tool_name": name,
                "tool_description": description,
                "parameters_schema": parameters_schema,
                "code": code,
                "sandbox_output": self._sandbox_output,
            },
        )
        log.info("add_tool_queued_for_hitl", name=name, nid=nid)
        return ToolResult(
            tool_name="add_tool",
            success=False,
            output=f"Tool registration queued for approval (id={nid})",
            awaiting_approval=True,
        )

    async def _open_pr(
        self, title: str, reason: str, branch_name: Optional[str]
    ) -> ToolResult:
        if not self._pending_patch:
            return ToolResult(
                tool_name="open_pr",
                success=False,
                output="",
                error="No pending patch. Call propose_patch first.",
            )
        if not self._sandbox_passed:
            return ToolResult(
                tool_name="open_pr",
                success=False,
                output="",
                error="Run sandbox_code first and ensure all CI checks pass.",
            )
        if not title:
            return ToolResult(
                tool_name="open_pr",
                success=False,
                output="",
                error="title is required.",
            )

        if not branch_name:
            branch_name = (
                "feat/"
                + title.lower()
                .replace(" ", "-")
                .replace("/", "-")
                .replace("'", "")
                .replace('"', "")[:50]
            )

        diff_lines: list[str] = []
        for rel_path, content in self._pending_patch.items():
            diff_lines.append(f"### {rel_path}\n```\n{content}\n```")
        diff_block = "\n\n".join(diff_lines)

        pr_body = (
            f"{reason}\n\n"
            "## Changes\n\n"
            f"{diff_block}\n\n"
            "## CI\n\n"
            f"```\n{self._sandbox_output}\n```\n\n"
            "## References\n\n"
            "- [ADR 007 — Agent-generated code proposals with sandboxed CI gate]"
            "(docs/decisions/007-code-proposals.md)\n\n"
            "🤖 Generated with [Pueo](https://github.com/AndysWorth/pueo-cases)"
        )

        from utils.card_types import CARD_TYPE_OPEN_PR

        nid = get_correlation_id() or str(uuid.uuid4())
        await self._notifier.send(
            subject=f"Pueo: open_pr approval required — {title}",
            body=f"Open GitHub PR: {title}\n\n{reason}",
            payload={
                "notification_id": nid,
                "card_type": CARD_TYPE_OPEN_PR,
                "pr_title": title,
                "pr_body": pr_body,
                "branch_name": branch_name,
                "patch_files": dict(self._pending_patch),
                "sandbox_output": self._sandbox_output,
            },
        )
        log.info("open_pr_queued_for_hitl", title=title, branch=branch_name, nid=nid)
        return ToolResult(
            tool_name="open_pr",
            success=False,
            output=f"PR queued for approval (id={nid}). Branch: {branch_name!r}",
            awaiting_approval=True,
        )

    async def _enrich_fix_context(
        self,
        original_config: str,
        proposed_yaml: str,
        description: str,
    ) -> "FixEnrichment | None":
        if self._llm_client is None:
            return None

        from utils.agent.tool_registry import FixEnrichment

        prompt = (
            "You are reviewing a proposed Home Assistant configuration fix.\n\n"
            f"Agent description: {description}\n\n"
            "Current configuration.yaml (excerpt):\n"
            f"```yaml\n{original_config[:3000]}\n```\n\n"
            "Proposed replacement:\n"
            f"```yaml\n{proposed_yaml[:3000]}\n```\n\n"
            "Identify the specific lines being changed, explain in plain English "
            "why the current configuration is wrong and how the fix addresses it, "
            "rate your confidence, and summarise the fix only if confidence is high."
        )
        try:
            import config as _cfg

            response = await self._llm_client.chat(
                model=_cfg.OLLAMA_MODEL,
                messages=[{"role": "user", "content": prompt}],
                options={"temperature": 0.0},
                format=FixEnrichment.model_json_schema(),
            )
            content = response.get("message", {}).get("content", "")
            return FixEnrichment.model_validate_json(content)
        except Exception as exc:
            log.warning("fix_enrichment_failed", error=str(exc))
            return None

    async def _apply_fix(self, yaml_content: str, description: str) -> ToolResult:
        if self._apply_fix_used:
            log.warning("apply_fix_already_used")
            return ToolResult(
                tool_name="apply_fix",
                success=False,
                output="",
                error="apply_fix may only be called once per loop run",
            )

        from utils.yaml_validator import validate_proposed_fix

        try:
            original = await self._ha_ssh.read_file(CONFIG_REMOTE_PATH)
        except Exception as exc:
            return ToolResult(
                tool_name="apply_fix",
                success=False,
                output="",
                error=f"Could not read original config: {exc}",
            )

        validation = validate_proposed_fix(original, yaml_content)
        if not validation.is_safe:
            log.error("apply_fix_validation_failed", reasons=validation.reasons)
            return ToolResult(
                tool_name="apply_fix",
                success=False,
                output="",
                error=f"Content validation failed: {'; '.join(validation.reasons)}",
            )

        enrichment = await self._enrich_fix_context(original, yaml_content, description)

        from utils.agent.autonomy import RiskLevel

        from utils.card_types import CARD_TYPE_REPAIR

        nid = get_correlation_id() or str(uuid.uuid4())
        if enrichment:
            body = f"{enrichment.explanation}"
            if enrichment.suggested_fix_summary:
                body += f"\n\nSuggested fix: {enrichment.suggested_fix_summary}"
            body += f"\n\nProposed YAML:\n{yaml_content[:500]}"
        else:
            body = f"Proposed fix:\n{yaml_content[:500]}"
        approved = await self._gate.queue_for_approval(
            subject=f"Pueo: apply_fix approval required — {description}",
            body=body,
            payload={
                "notification_id": nid,
                "card_type": CARD_TYPE_REPAIR,
                "severity": "HIGH",
                "description": description,
                "correlation_id": nid,
                "pending_fix_yaml": yaml_content,
                "pending_fix_description": description,
                "enrichment": enrichment.model_dump() if enrichment else None,
            },
            notifier=self._notifier,
            risk=RiskLevel.HIGH,
        )
        if not approved:
            # Fix is queued for human review; the agent loop exits so the
            # dashboard can execute the fix when the human approves.
            self._apply_fix_used = True
            log.info("apply_fix_queued_for_hitl", nid=nid)
            return ToolResult(
                tool_name="apply_fix",
                success=False,
                output=f"Fix queued for approval (id={nid}); agent loop exiting",
                awaiting_approval=True,
            )

        # Backup invariant: backup before every write
        from agents.ha_agent_advanced import (
            enforce_ha_retention,
            execute_remote_backup,
            offload_backup_to_local,
            purge_local_backups,
            record_backup_slug,
        )
        from agents.ha_agent_sandbox_engine import (
            commit_atomic_swap,
            deploy_and_test_in_sandbox,
        )

        try:
            slug = await execute_remote_backup(ssh_client=self._ha_ssh)
            record_backup_slug(slug)
            await offload_backup_to_local(slug, ssh_client=self._ha_ssh)
            await enforce_ha_retention(ssh_client=self._ha_ssh)
            purge_local_backups()
        except Exception as exc:
            log.critical("apply_fix_backup_failed", error=str(exc))
            return ToolResult(
                tool_name="apply_fix",
                success=False,
                output="",
                error=f"Backup failed: {exc}",
            )

        passed = await deploy_and_test_in_sandbox(yaml_content, ssh_client=self._ha_ssh)
        if not passed:
            return ToolResult(
                tool_name="apply_fix",
                success=False,
                output="",
                error="Sandbox test failed; production not modified",
            )

        await commit_atomic_swap(yaml_content, ssh_client=self._ha_ssh)
        self._apply_fix_used = True
        log.info("apply_fix_committed", slug=slug)
        return ToolResult(
            tool_name="apply_fix",
            success=True,
            output=f"Fix applied and validated; backup slug: {slug}",
        )

    async def _verify_fix(self) -> ToolResult:
        from agents.ha_agent_sandbox_engine import execute_remote_preflight_check

        try:
            exit_code, stdout, stderr = await execute_remote_preflight_check(
                ssh_client=self._ha_ssh
            )
            if exit_code == 0:
                return ToolResult(
                    tool_name="verify_fix", success=True, output="ha core check passed"
                )
            return ToolResult(
                tool_name="verify_fix",
                success=False,
                output=stderr or stdout,
                error="ha core check failed",
            )
        except Exception as exc:
            return ToolResult(
                tool_name="verify_fix", success=False, output="", error=str(exc)
            )

    # ------------------------------------------------------------------
    # NetAlertX tools
    # ------------------------------------------------------------------

    async def _query_netalertx(self, query_type: str) -> ToolResult:
        if self._api is None:
            return ToolResult(
                tool_name="query_netalertx",
                success=False,
                output="",
                error="NetAlertX API client not configured",
            )
        try:
            data: dict | list
            if query_type == "health":
                data = await self._api.get_about()
            elif query_type == "devices":
                data = await self._api.get_devices()
            elif query_type == "events":
                data = await self._api.get_events()
            else:
                return ToolResult(
                    tool_name="query_netalertx",
                    success=False,
                    output="",
                    error=f"Unknown query_type: {query_type!r}",
                )
            return ToolResult(
                tool_name="query_netalertx",
                success=True,
                output=json.dumps(data, default=str),
            )
        except Exception as exc:
            return ToolResult(
                tool_name="query_netalertx", success=False, output="", error=str(exc)
            )

    async def _investigate_device(self, ip: str) -> ToolResult:
        """Enrich an IP address with MAC, vendor, hostname, and NetAlertX context."""
        import re

        from agents.ha_notification_manager import enrich_http_login

        if not ip or not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", ip):
            return ToolResult(
                tool_name="investigate_device",
                success=False,
                output="",
                error=f"Invalid IP address: {ip!r}",
            )
        try:
            context = await enrich_http_login(
                ip,
                netalertx_client=self._api,
                ws_client=self._ws_client,
            )
            return ToolResult(
                tool_name="investigate_device",
                success=True,
                output=json.dumps(context, default=str),
            )
        except Exception as exc:
            return ToolResult(
                tool_name="investigate_device",
                success=False,
                output="",
                error=str(exc),
            )

    async def _switch_model(self, model_name: Optional[str]) -> ToolResult:
        """Auto-select or explicitly set the active Ollama model."""
        from utils.hardware import (
            apply_model_selection,
            detect_local_hardware,
            list_ollama_models,
            recommend_model,
            select_best_model,
        )

        previous = _config_mod.OLLAMA_MODEL
        try:
            if model_name:
                available = await asyncio.to_thread(list_ollama_models)
                names = [m.name for m in available]
                if model_name not in names:
                    return ToolResult(
                        tool_name="switch_model",
                        success=False,
                        output="",
                        error=f"Model {model_name!r} is not installed. "
                        f"Installed: {', '.join(names) or 'none'}",
                    )
                selected = model_name
            else:
                profile = await asyncio.to_thread(detect_local_hardware)
                available = await asyncio.to_thread(list_ollama_models)
                candidate = recommend_model(profile, available)
                if candidate is None:
                    return ToolResult(
                        tool_name="switch_model",
                        success=False,
                        output="",
                        error="No suitable tools-capable model found for this hardware.",
                    )
                selected = candidate

            await asyncio.to_thread(apply_model_selection, selected)
            profile = await asyncio.to_thread(detect_local_hardware)
            result = {
                "previous_model": previous,
                "selected_model": selected,
                "hardware": {"chip": profile.chip, "ram_gb": profile.ram_gb},
                "auto_selected": model_name is None,
            }
            import json as _json

            return ToolResult(
                tool_name="switch_model", success=True, output=_json.dumps(result)
            )
        except Exception as exc:
            return ToolResult(
                tool_name="switch_model", success=False, output="", error=str(exc)
            )

    async def _restart_netalertx(self) -> ToolResult:
        ssh = self._nax_ssh or self._ha_ssh
        try:
            _, stdout, stderr = await ssh.run(
                f"docker restart {self._container}", check=False
            )
            if self._api is not None:
                try:
                    await self._api.trigger_scan()
                except Exception:  # nosec B110 — scan trigger is best-effort
                    pass
            return ToolResult(
                tool_name="restart_netalertx",
                success=True,
                output=stdout or stderr or "container restarted",
            )
        except Exception as exc:
            return ToolResult(
                tool_name="restart_netalertx", success=False, output="", error=str(exc)
            )

    async def _rewrite_netalertx_conf(self, overrides: dict) -> ToolResult:
        from netalertx.config_validator import validate_app_conf
        from netalertx.healer import _merge_conf

        _CONF_PATH = "/data/app.conf"
        ssh = self._nax_ssh or self._ha_ssh
        try:
            current = await ssh.read_file(_CONF_PATH)
        except (FileNotFoundError, OSError):
            current = ""

        fixed = _merge_conf(current, {k: str(v) for k, v in overrides.items()})
        issues = validate_app_conf(fixed)
        blocking = [i for i in issues if i.severity in ("HIGH", "CRITICAL")]
        if blocking:
            reasons = [i.message for i in blocking]
            return ToolResult(
                tool_name="rewrite_netalertx_conf",
                success=False,
                output="",
                error=f"Config still invalid after merge: {'; '.join(reasons)}",
            )

        try:
            await ssh.write_file(_CONF_PATH, fixed)
            return ToolResult(
                tool_name="rewrite_netalertx_conf",
                success=True,
                output="app.conf updated",
            )
        except Exception as exc:
            return ToolResult(
                tool_name="rewrite_netalertx_conf",
                success=False,
                output="",
                error=str(exc),
            )
