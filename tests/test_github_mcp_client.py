"""Exercise the real MCP SDK transport against an offline stdio server."""

import json
from pathlib import Path
import sys
from types import SimpleNamespace
import asyncio

import pytest

from pony.mcp import github_client as github


def test_github_mcp_stdio_round_trip_and_repo_boundary(monkeypatch, tmp_path):
    mcp = pytest.importorskip("mcp")
    original_parameters = mcp.StdioServerParameters
    fixture_script = Path(__file__).with_name("fake_github_mcp_server.py")

    def fixture_parameters(**kwargs):
        return original_parameters(
            command=sys.executable,
            args=["-u", str(fixture_script)],
            env=kwargs["env"],
        )

    monkeypatch.setattr(mcp, "StdioServerParameters", fixture_parameters)
    monkeypatch.setattr(
        github,
        "validate_github_server_path",
        lambda _path, _root: Path(sys.executable),
    )
    client = github.GitHubMCPClient(
        server_path=sys.executable,
        repo="wujing123666/pony-code",
        workspace_root=tmp_path,
        token="synthetic-test-token",
    ).start()
    try:
        result = client.call_tool(
            "pull_request_read",
            {
                "method": "get_check_runs",
                "owner": "wujing123666",
                "repo": "pony-code",
                "pullNumber": 12,
            },
        )
        assert json.loads(result) == {
            "method": "get_check_runs",
            "owner": "wujing123666",
            "repo": "pony-code",
            "number": 12,
        }
        with pytest.raises(github.GitHubMCPError, match="github_mcp_repo_denied"):
            client.call_tool(
                "issue_read",
                {"method": "get", "owner": "other", "repo": "pony-code", "issue_number": 1},
            )
    finally:
        client.close()
    assert not client._thread.is_alive()
    with pytest.raises(github.GitHubMCPError, match="github_mcp_disconnected"):
        client.call_tool("issue_read", {})


@pytest.mark.parametrize(
    "result,code",
    [
        (
            SimpleNamespace(
                content=[SimpleNamespace(type="text", text="not found")],
                structured_content=None,
                is_error=True,
            ),
            "github_mcp_tool_error",
        ),
        (
            SimpleNamespace(
                content=[SimpleNamespace(type="image", text="")],
                structured_content=None,
                is_error=False,
            ),
            "github_mcp_result_unsupported",
        ),
        (
            SimpleNamespace(
                content=[SimpleNamespace(type="text", text="x" * (github.MAX_MCP_RESULT_BYTES + 1))],
                structured_content=None,
                is_error=False,
            ),
            "github_mcp_result_too_large",
        ),
    ],
)
def test_mcp_result_errors_are_not_reported_as_success(result, code, tmp_path, monkeypatch):
    monkeypatch.setattr(github, "validate_github_server_path", lambda _path, _root: tmp_path)
    client = github.GitHubMCPClient(
        server_path=tmp_path,
        repo="owner/repo",
        workspace_root=tmp_path,
        token="synthetic-test-token",
    )

    async def return_result(_name, _arguments, **_kwargs):
        return result

    client._client = SimpleNamespace(call_tool=return_result)
    with pytest.raises(github.GitHubMCPError) as caught:
        asyncio.run(client._call_tool("issue_read", {}))
    assert caught.value.code == code


def test_mcp_text_resource_is_included_in_file_result(tmp_path, monkeypatch):
    monkeypatch.setattr(github, "validate_github_server_path", lambda _path, _root: tmp_path)
    client = github.GitHubMCPClient(
        server_path=tmp_path,
        repo="owner/repo",
        workspace_root=tmp_path,
        token="synthetic-test-token",
    )

    async def return_result(_name, _arguments, **_kwargs):
        return SimpleNamespace(
            content=[
                SimpleNamespace(type="text", text="file metadata"),
                SimpleNamespace(
                    type="resource",
                    resource=SimpleNamespace(
                        mime_type="text/plain; charset=utf-8", text="actual file body"
                    ),
                ),
            ],
            structured_content=None,
            is_error=False,
        )

    client._client = SimpleNamespace(call_tool=return_result)
    assert asyncio.run(client._call_tool("get_file_contents", {})) == (
        "file metadata\nactual file body"
    )


def test_mcp_protocol_tool_error_has_tool_error_code(tmp_path, monkeypatch):
    from mcp import MCPError

    monkeypatch.setattr(github, "validate_github_server_path", lambda _path, _root: tmp_path)
    client = github.GitHubMCPClient(
        server_path=tmp_path,
        repo="owner/repo",
        workspace_root=tmp_path,
        token="synthetic-test-token",
    )

    async def raise_tool_error(_name, _arguments, **_kwargs):
        raise MCPError(-32000, "remote object was not found")

    client._client = SimpleNamespace(call_tool=raise_tool_error)
    with pytest.raises(github.GitHubMCPError) as caught:
        asyncio.run(client._call_tool("issue_read", {}))
    assert caught.value.code == "github_mcp_tool_error"
    assert "remote object was not found" in str(caught.value)


def test_mcp_schema_mismatch_has_stable_error(tmp_path, monkeypatch):
    monkeypatch.setattr(github, "validate_github_server_path", lambda _path, _root: tmp_path)
    client = github.GitHubMCPClient(
        server_path=tmp_path,
        repo="owner/repo",
        workspace_root=tmp_path,
        token="synthetic-test-token",
    )
    tools = [
        SimpleNamespace(
            name="pull_request_read",
            input_schema={
                "properties": {
                    "method": "malformed",
                    "owner": {},
                    "repo": {},
                    "pullNumber": {},
                }
            },
        ),
        SimpleNamespace(
            name="issue_read",
            input_schema={
                "properties": {
                    "method": {"const": "get"},
                    "owner": {},
                    "repo": {},
                    "issue_number": {},
                }
            },
        ),
        SimpleNamespace(
            name="get_file_contents",
            input_schema={"properties": {"owner": {}, "repo": {}, "path": {}}},
        ),
    ]

    async def list_tools(**_kwargs):
        return SimpleNamespace(tools=tools, next_cursor=None)

    with pytest.raises(github.GitHubMCPError) as caught:
        asyncio.run(client._check_tools(SimpleNamespace(list_tools=list_tools)))
    assert caught.value.code == "github_mcp_schema_mismatch"


def test_close_before_worker_loop_prevents_connection_start(tmp_path, monkeypatch):
    monkeypatch.setattr(github, "validate_github_server_path", lambda _path, _root: tmp_path)
    client = github.GitHubMCPClient(
        server_path=tmp_path,
        repo="owner/repo",
        workspace_root=tmp_path,
        token="synthetic-test-token",
    )

    client.close()
    client._thread_main()

    assert client._task is None
    assert client._client is None
    assert client._ready.is_set()
    with pytest.raises(github.GitHubMCPError) as caught:
        client.start()
    assert caught.value.code == "github_mcp_disconnected"
