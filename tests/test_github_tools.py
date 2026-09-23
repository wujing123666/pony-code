"""Offline contracts for GitHub-only, read-only Pony tools."""

from types import SimpleNamespace

import pytest

from pony.mcp.github_client import validate_github_repo, validate_github_server_path
from pony.tools.github import (
    tool_github_read_file,
    tool_github_read_issue,
    tool_github_read_pr,
    validate_github_tool,
)
from pony.tools.registry import build_tool_registry


class FakeGitHubClient:
    owner = "wujing123666"
    repo = "pony-code"

    def __init__(self):
        self.calls = []

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return "remote result"


@pytest.mark.parametrize(
    "name,args",
    [
        ("github_read_pr", {"number": True}),
        ("github_read_pr", {"number": 0}),
        ("github_read_pr", {"number": 3, "view": "merge"}),
        ("github_read_pr", {"number": 3, "owner": "other"}),
        ("github_read_issue", {"number": 3, "repo": "other"}),
        ("github_read_file", {"path": "../secret.txt"}),
        ("github_read_file", {"path": ".env"}),
        ("github_read_file", {"path": "src\\settings.py"}),
        ("github_read_file", {"path": "README.md", "repo": "other"}),
    ],
)
def test_github_tools_reject_invalid_arguments(name, args):
    with pytest.raises(ValueError):
        validate_github_tool(name, args)


def test_github_tools_map_only_fixed_repo_and_read_methods():
    client = FakeGitHubClient()
    context = SimpleNamespace(github_mcp_client=client)
    assert tool_github_read_pr(context, {"number": 12, "view": "checks"}) == "remote result"
    assert tool_github_read_issue(context, {"number": 7}) == "remote result"
    assert tool_github_read_file(context, {"path": "README.md"}) == "remote result"
    assert client.calls == [
        (
            "pull_request_read",
            {
                "method": "get_check_runs",
                "owner": client.owner,
                "repo": client.repo,
                "pullNumber": 12,
            },
        ),
        (
            "issue_read",
            {
                "method": "get",
                "owner": client.owner,
                "repo": client.repo,
                "issue_number": 7,
            },
        ),
        (
            "get_file_contents",
            {"owner": client.owner, "repo": client.repo, "path": "README.md"},
        ),
    ]


def test_registry_only_exposes_github_when_configured():
    client = FakeGitHubClient()
    context = SimpleNamespace(
        github_mcp_client=None,
        trusted_executables={},
        depth=1,
        max_depth=1,
    )
    assert "github_read_pr" not in build_tool_registry(context)
    context.github_mcp_client = client
    tools = build_tool_registry(context)
    assert {"github_read_pr", "github_read_issue", "github_read_file"} <= set(tools)
    assert all(tools[name]["effect_class"] == "read_only" for name in tools if name.startswith("github_"))
    assert tools["github_read_pr"]["run"]({"number": 12}) == "remote result"


def test_repo_and_server_path_validation(tmp_path):
    assert validate_github_repo("wujing123666/pony-code") == ("wujing123666", "pony-code")
    with pytest.raises(ValueError, match="github_mcp_repo_invalid"):
        validate_github_repo("other/../repo")
    with pytest.raises(ValueError, match="github_mcp_server_invalid"):
        validate_github_server_path("relative.exe", tmp_path)
    inside = tmp_path / "server.exe"
    inside.write_bytes(b"fake")
    with pytest.raises(ValueError, match="github_mcp_server_invalid"):
        validate_github_server_path(inside, tmp_path)
