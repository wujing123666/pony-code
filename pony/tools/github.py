"""Explicit Pony tool wrappers for read-only GitHub MCP operations."""

from pony.security.paths import is_sensitive_path


GITHUB_TOOL_SPECS = {
    "github_read_pr": {
        "schema": {
            "number": {"type": "integer", "minimum": 1},
            "view": {
                "type": "string",
                "enum": ["details", "diff", "checks"],
                "default": "details",
            },
        },
        "risky": False,
        "effect_class": "read_only",
        "description": "Read a pull request, its diff, or its CI checks in the configured GitHub repository.",
    },
    "github_read_issue": {
        "schema": {"number": {"type": "integer", "minimum": 1}},
        "risky": False,
        "effect_class": "read_only",
        "description": "Read an issue in the configured GitHub repository.",
    },
    "github_read_file": {
        "schema": {"path": {"type": "string", "minLength": 1, "maxLength": 1024}},
        "risky": False,
        "effect_class": "read_only",
        "description": "Read one non-sensitive file from the configured GitHub repository.",
    },
}

_PR_METHODS = {
    "details": "get",
    "diff": "get_diff",
    "checks": "get_check_runs",
}


def validate_github_tool(name, args):
    if type(args) is not dict:
        raise ValueError("github tool arguments must be an object")
    if name == "github_read_pr":
        if set(args) - {"number", "view"}:
            raise ValueError("github_read_pr accepts only number and view")
        _positive_number(args.get("number"))
        view = args.get("view", "details")
        if type(view) is not str or view not in _PR_METHODS:
            raise ValueError("invalid pull request view")
        return
    if name == "github_read_issue":
        if set(args) != {"number"}:
            raise ValueError("github_read_issue requires only number")
        _positive_number(args.get("number"))
        return
    if name == "github_read_file":
        if set(args) != {"path"}:
            raise ValueError("github_read_file requires only path")
        _remote_file_path(args.get("path"))
        return
    raise ValueError("unknown github tool")


def _positive_number(value):
    if type(value) is not int or not 1 <= value <= 1_000_000_000:
        raise ValueError("number must be a positive GitHub issue or PR number")
    return value


def _remote_file_path(value):
    if type(value) is not str or not 1 <= len(value) <= 1024:
        raise ValueError("invalid GitHub file path")
    if (
        value.startswith("/")
        or "\\" in value
        or "%" in value
        or ":" in value
        or any(ord(character) < 32 for character in value)
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError("invalid GitHub file path")
    if is_sensitive_path(value):
        raise ValueError("sensitive_path")
    return value


def tool_github_read_pr(context, args):
    validate_github_tool("github_read_pr", args)
    client = context.github_mcp_client
    return client.call_tool(
        "pull_request_read",
        {
            "method": _PR_METHODS[args.get("view", "details")],
            "owner": client.owner,
            "repo": client.repo,
            "pullNumber": args["number"],
        },
    )


def tool_github_read_issue(context, args):
    validate_github_tool("github_read_issue", args)
    client = context.github_mcp_client
    return client.call_tool(
        "issue_read",
        {
            "method": "get",
            "owner": client.owner,
            "repo": client.repo,
            "issue_number": args["number"],
        },
    )


def tool_github_read_file(context, args):
    validate_github_tool("github_read_file", args)
    client = context.github_mcp_client
    return client.call_tool(
        "get_file_contents",
        {
            "owner": client.owner,
            "repo": client.repo,
            "path": args["path"],
        },
    )
