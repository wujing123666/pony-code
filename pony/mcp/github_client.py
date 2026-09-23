"""One bounded stdio connection to the official GitHub MCP Server."""

import asyncio
from concurrent.futures import TimeoutError as FutureTimeoutError
import json
import os
from pathlib import Path
import re
import threading


GITHUB_TOKEN_ENV_NAME = "GITHUB_PERSONAL_ACCESS_TOKEN"
GITHUB_REMOTE_TOOLS = frozenset(
    {"pull_request_read", "issue_read", "get_file_contents"}
)
MAX_MCP_RESULT_BYTES = 4 * 1024 * 1024
_START_TIMEOUT_SECONDS = 20
_CALL_TIMEOUT_SECONDS = 30
_CLOSE_TIMEOUT_SECONDS = 5
_REPO_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}"
)
_REQUIRED_REMOTE_FIELDS = {
    "pull_request_read": {"method", "owner", "repo", "pullNumber"},
    "issue_read": {"method", "owner", "repo", "issue_number"},
    "get_file_contents": {"owner", "repo", "path"},
}


class GitHubMCPError(RuntimeError):
    def __init__(self, code, message=None):
        self.code = code
        super().__init__(message or code)


def _nested_github_error(exc):
    if isinstance(exc, GitHubMCPError):
        return exc
    if isinstance(exc, BaseExceptionGroup):
        for nested in exc.exceptions:
            found = _nested_github_error(nested)
            if found is not None:
                return found
    return None


def validate_github_repo(value):
    if type(value) is not str or not _REPO_RE.fullmatch(value):
        raise ValueError("github_mcp_repo_invalid")
    owner, repo = value.split("/", 1)
    if owner in {".", ".."} or repo in {".", ".."}:
        raise ValueError("github_mcp_repo_invalid")
    return owner, repo


def validate_github_server_path(value, workspace_root):
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("github_mcp_server_invalid")
    try:
        resolved = path.resolve(strict=True)
        root = Path(workspace_root).resolve(strict=True)
        if resolved != path or resolved.is_relative_to(root):
            raise ValueError("github_mcp_server_invalid")
        if not resolved.is_file() or resolved.is_symlink():
            raise ValueError("github_mcp_server_invalid")
        if os.name == "nt" and resolved.suffix.casefold() != ".exe":
            raise ValueError("github_mcp_server_invalid")
    except (OSError, RuntimeError):
        raise ValueError("github_mcp_server_invalid") from None
    return resolved


class GitHubMCPClient:
    """Own the async MCP session while synchronous Pony tools call into it."""

    def __init__(self, *, server_path, repo, workspace_root, token):
        self.server_path = validate_github_server_path(server_path, workspace_root)
        self.owner, self.repo = validate_github_repo(repo)
        if type(token) is not str or not token.strip():
            raise GitHubMCPError("github_mcp_token_missing")
        self._token = token
        self._thread = None
        self._loop = None
        self._task = None
        self._stop = None
        self._client = None
        self._ready = threading.Event()
        self._closing = threading.Event()
        self._startup_error = None
        self._closed = False

    def start(self):
        if self._closed:
            raise GitHubMCPError("github_mcp_disconnected")
        if self._thread is not None:
            raise GitHubMCPError("github_mcp_already_started")
        self._thread = threading.Thread(
            target=self._thread_main,
            name="pony-github-mcp",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(_START_TIMEOUT_SECONDS):
            self.close()
            raise GitHubMCPError("github_mcp_start_timeout")
        if self._startup_error is not None:
            self.close()
            raise self._startup_error
        if self._client is None:
            self.close()
            raise GitHubMCPError("github_mcp_disconnected")
        return self

    def _thread_main(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            if self._closing.is_set():
                return
            self._task = loop.create_task(self._serve())
            loop.run_until_complete(self._task)
        except BaseException:
            if not self._ready.is_set():
                self._startup_error = GitHubMCPError("github_mcp_start_failed")
        finally:
            self._client = None
            self._ready.set()
            loop.close()
            asyncio.set_event_loop(None)

    async def _serve(self):
        try:
            from mcp import Client, StdioServerParameters, stdio_client
        except ImportError:
            self._startup_error = GitHubMCPError("github_mcp_sdk_missing")
            self._ready.set()
            return

        self._stop = asyncio.Event()
        if self._closing.is_set():
            return
        params = StdioServerParameters(
            command=str(self.server_path),
            args=[
                "stdio",
                "--tools=get_file_contents,issue_read,pull_request_read",
                "--read-only",
            ],
            env={GITHUB_TOKEN_ENV_NAME: self._token},
        )
        # Server stderr is untrusted and may echo credentials or private content.
        try:
            with open(os.devnull, "w", encoding="utf-8") as errlog:
                async with Client(
                    stdio_client(params, errlog=errlog),
                    read_timeout_seconds=_CALL_TIMEOUT_SECONDS,
                    input_required_max_rounds=0,
                ) as client:
                    await self._check_tools(client)
                    if self._closing.is_set():
                        return
                    self._client = client
                    self._ready.set()
                    await self._stop.wait()
        except Exception as exc:
            if not self._ready.is_set():
                self._startup_error = _nested_github_error(exc) or GitHubMCPError(
                    "github_mcp_start_failed"
                )
        finally:
            self._client = None
            self._ready.set()

    async def _check_tools(self, client):
        found = {}
        cursor = None
        for _ in range(16):
            listing = await client.list_tools(cursor=cursor)
            for tool in listing.tools:
                if tool.name in GITHUB_REMOTE_TOOLS:
                    found[tool.name] = tool.input_schema
            cursor = listing.next_cursor
            if cursor is None:
                break
        else:
            raise GitHubMCPError("github_mcp_tool_list_too_large")
        if set(found) != GITHUB_REMOTE_TOOLS:
            raise GitHubMCPError("github_mcp_tools_missing")
        for name, schema in found.items():
            if not isinstance(schema, dict):
                raise GitHubMCPError("github_mcp_schema_mismatch")
            properties = schema.get("properties")
            if not isinstance(properties, dict):
                raise GitHubMCPError("github_mcp_schema_mismatch")
            if not _REQUIRED_REMOTE_FIELDS[name] <= set(properties):
                raise GitHubMCPError("github_mcp_schema_mismatch")
        for name, expected in (
            ("pull_request_read", {"get", "get_diff", "get_check_runs"}),
            ("issue_read", {"get"}),
        ):
            method_schema = found[name]["properties"]["method"]
            if not isinstance(method_schema, dict):
                raise GitHubMCPError("github_mcp_schema_mismatch")
            methods = method_schema.get("enum", ())
            if "const" in method_schema:
                methods = (*methods, method_schema["const"])
            if not isinstance(methods, (list, tuple)) or not expected <= set(methods):
                raise GitHubMCPError("github_mcp_schema_mismatch")

    def call_tool(self, name, arguments):
        if (
            self._closed
            or self._client is None
            or self._loop is None
            or not self._loop.is_running()
        ):
            raise GitHubMCPError("github_mcp_disconnected")
        if name not in GITHUB_REMOTE_TOOLS:
            raise GitHubMCPError("github_mcp_tool_unavailable")
        if (
            type(arguments) is not dict
            or arguments.get("owner") != self.owner
            or arguments.get("repo") != self.repo
        ):
            raise GitHubMCPError("github_mcp_repo_denied")
        future = asyncio.run_coroutine_threadsafe(self._call_tool(name, arguments), self._loop)
        try:
            return future.result(timeout=_CALL_TIMEOUT_SECONDS + 2)
        except FutureTimeoutError:
            future.cancel()
            raise GitHubMCPError("github_mcp_timeout") from None
        except GitHubMCPError:
            raise
        except Exception:
            raise GitHubMCPError("github_mcp_call_failed") from None

    async def _call_tool(self, name, arguments):
        from mcp import MCPError

        try:
            result = await self._client.call_tool(
                name, arguments, read_timeout_seconds=_CALL_TIMEOUT_SECONDS
            )
        except MCPError as exc:
            raise GitHubMCPError("github_mcp_tool_error", str(exc)[:512]) from None
        except Exception:
            raise GitHubMCPError("github_mcp_call_failed") from None
        parts = []
        total_bytes = 0
        for item in result.content:
            if item.type == "text":
                text = item.text
            elif item.type == "resource":
                resource = item.resource
                mime_type = getattr(resource, "mime_type", None)
                text = getattr(resource, "text", None)
                if (
                    not isinstance(text, str)
                    or not isinstance(mime_type, str)
                    or not mime_type.lower().startswith("text/")
                ):
                    raise GitHubMCPError("github_mcp_result_unsupported")
            else:
                raise GitHubMCPError("github_mcp_result_unsupported")
            if not isinstance(text, str):
                raise GitHubMCPError("github_mcp_result_unsupported")
            total_bytes += len(text.encode("utf-8")) + 1
            if total_bytes > MAX_MCP_RESULT_BYTES:
                raise GitHubMCPError("github_mcp_result_too_large")
            parts.append(text)
        if not parts and result.structured_content is not None:
            encoded = json.dumps(result.structured_content, ensure_ascii=False)
            if len(encoded.encode("utf-8")) > MAX_MCP_RESULT_BYTES:
                raise GitHubMCPError("github_mcp_result_too_large")
            parts.append(encoded)
        content = "\n".join(parts)
        if result.is_error:
            raise GitHubMCPError("github_mcp_tool_error", content[:512])
        return content

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._closing.set()
        loop = self._loop
        thread = self._thread
        if loop is not None and loop.is_running():
            if self._stop is not None:
                loop.call_soon_threadsafe(self._stop.set)
            elif self._task is not None:
                loop.call_soon_threadsafe(self._task.cancel)
        if thread is not None and thread is not threading.current_thread():
            thread.join(_CLOSE_TIMEOUT_SECONDS)
            if (
                thread.is_alive()
                and loop is not None
                and loop.is_running()
                and self._task is not None
            ):
                loop.call_soon_threadsafe(self._task.cancel)
                thread.join(_CLOSE_TIMEOUT_SECONDS)
