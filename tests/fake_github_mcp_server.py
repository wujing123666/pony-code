"""Offline stdio fixture for the optional GitHub MCP client tests."""

import json
from typing import Literal

from mcp.server import MCPServer


server = MCPServer("fake-github")


@server.tool()
def pull_request_read(
    method: Literal["get", "get_diff", "get_check_runs"],
    owner: str,
    repo: str,
    pullNumber: int,
) -> str:
    return json.dumps(
        {"method": method, "owner": owner, "repo": repo, "number": pullNumber}
    )


@server.tool()
def issue_read(
    method: Literal["get"], owner: str, repo: str, issue_number: int
) -> str:
    return json.dumps(
        {"method": method, "owner": owner, "repo": repo, "number": issue_number}
    )


@server.tool()
def get_file_contents(owner: str, repo: str, path: str) -> str:
    return json.dumps({"owner": owner, "repo": repo, "path": path})


if __name__ == "__main__":
    server.run(transport="stdio")
