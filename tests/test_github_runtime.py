"""Pony executor integration for an opt-in GitHub MCP connection."""

import pytest

from benchmarks.support.fake_provider import FakeModelClient
from pony import Pony
from pony.config.environment import write_project_env_assignments
from pony.mcp.github_client import GitHubMCPError
from pony.runtime.application import _build_redaction_snapshot
from pony.runtime.options import GitHubMCPSettings, RuntimeOptions
from pony.state.session_store import SessionStore
from pony.workspace.context import WorkspaceContext


class FakeGitHubMCPClient:
    def __init__(self, *, server_path, repo, workspace_root, token):
        self.owner, self.repo = repo.split("/", 1)
        self.token = token
        self.calls = []
        self.closed = False

    def start(self):
        return self

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return f"remote result; token={self.token}"

    def close(self):
        self.closed = True


@pytest.mark.parametrize("custom_redaction_env", [None, {}])
def test_github_tool_runs_through_existing_executor_and_redacts_token(
    tmp_path, monkeypatch, custom_redaction_env
):
    from pony.runtime import application

    monkeypatch.setattr(application, "GitHubMCPClient", FakeGitHubMCPClient)
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "synthetic-token-123456")
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    agent = Pony(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".pony" / "sessions"),
        options=RuntimeOptions(
            project_trusted=True,
            github_mcp=GitHubMCPSettings("C:/tools/server.exe", "owner/repo"),
            redaction_env=custom_redaction_env,
        ),
    )
    client = agent.github_mcp_client
    try:
        assert "github_read_pr" in agent.visible_tools()
        result = agent.execute_tool("github_read_pr", {"number": 12, "view": "checks"})
        assert result.metadata["tool_status"] == "ok"
        assert "synthetic-token-123456" not in result.content
        assert client.calls[0][0] == "pull_request_read"
        assert client.calls[0][1]["method"] == "get_check_runs"
        invalid = agent.execute_tool("github_read_file", {"path": ".env"})
        assert invalid.metadata["tool_status"] == "rejected"
        assert len(client.calls) == 1
    finally:
        agent.close()
    assert client.closed


def test_github_token_from_private_project_env_survives_restart(tmp_path, monkeypatch):
    from pony.runtime import application

    monkeypatch.setattr(application, "GitHubMCPClient", FakeGitHubMCPClient)
    monkeypatch.delenv("GITHUB_PERSONAL_ACCESS_TOKEN", raising=False)
    write_project_env_assignments(
        tmp_path, {"GITHUB_PERSONAL_ACCESS_TOKEN": "synthetic-project-token-123456"}
    )
    workspace = WorkspaceContext.build(tmp_path)

    for _ in range(2):
        agent = Pony(
            model_client=FakeModelClient([]),
            workspace=workspace,
            session_store=SessionStore(tmp_path / ".pony" / "sessions"),
            options=RuntimeOptions(
                project_trusted=True,
                github_mcp=GitHubMCPSettings("C:/tools/server.exe", "owner/repo"),
            ),
        )
        try:
            assert agent.github_mcp_client.token == "synthetic-project-token-123456"
            result = agent.execute_tool("github_read_file", {"path": "README.md"})
            assert result.metadata["tool_status"] == "ok"
            assert "synthetic-project-token-123456" not in result.content
        finally:
            agent.close()


def test_project_env_token_takes_precedence_and_redacts_process_token(
    tmp_path, monkeypatch
):
    from pony.runtime import application

    monkeypatch.setattr(application, "GitHubMCPClient", FakeGitHubMCPClient)
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "synthetic-process-token-123456")
    write_project_env_assignments(
        tmp_path, {"GITHUB_PERSONAL_ACCESS_TOKEN": "synthetic-project-token-123456"}
    )
    snapshot, _, _ = _build_redaction_snapshot(tmp_path)
    agent = Pony(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".pony" / "sessions"),
        options=RuntimeOptions(
            project_trusted=True,
            github_mcp=GitHubMCPSettings("C:/tools/server.exe", "owner/repo"),
            redaction_env=snapshot,
            trusted_redaction_env=True,
        ),
    )
    try:
        assert agent.github_mcp_client.token == "synthetic-project-token-123456"
        redacted = agent.redact_text(
            "synthetic-project-token-123456 synthetic-process-token-123456"
        )
        assert "synthetic-project-token-123456" not in redacted
        assert "synthetic-process-token-123456" not in redacted
    finally:
        agent.close()


def test_github_mcp_error_message_is_redacted(tmp_path, monkeypatch):
    from pony.runtime import application

    class FailingGitHubMCPClient(FakeGitHubMCPClient):
        def call_tool(self, name, arguments):
            raise GitHubMCPError(
                "github_mcp_tool_error",
                f"remote error included credential {self.token}",
            )

    monkeypatch.setattr(application, "GitHubMCPClient", FailingGitHubMCPClient)
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "synthetic-error-token-123456")
    agent = Pony(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".pony" / "sessions"),
        options=RuntimeOptions(
            project_trusted=True,
            github_mcp=GitHubMCPSettings("C:/tools/server.exe", "owner/repo"),
        ),
    )
    try:
        result = agent.execute_tool("github_read_issue", {"number": 12})
        assert result.metadata["tool_status"] == "error"
        assert result.metadata["tool_error_code"] == "github_mcp_tool_error"
        assert "synthetic-error-token-123456" not in result.content
    finally:
        agent.close()
