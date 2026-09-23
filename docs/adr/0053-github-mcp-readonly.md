# ADR-0053: Explicit read-only GitHub MCP tools

## Status

Implemented on the GitHub MCP integration branch. Offline contract evidence and
live GitHub API evidence are reported separately.

## Context

Pony registers model-visible tools statically and executes a synchronous
`runner(args)` only after its own schema, permission, and secret checks. MCP
servers instead expose `tools/list` and `tools/call` over a transport. Directly
importing an arbitrary server's tool catalog would bypass Pony's reviewed
argument validators, enlarge the model prompt, and turn server-provided
annotations into authority. GitHub PR, issue, and repository reads are useful
remote context that local workspace tools cannot provide.

## Decision

- GitHub MCP is opt-in for top-level `run` and `repl`. The user supplies an
  absolute path to the official local executable and one `owner/repo` target.
  The executable must be a regular file outside the workspace, with no symlink
  resolution; this is a user-selected Host program, not a sandboxed dependency.
  GitHub MCP settings live only in `RuntimeOptions`, not in tracked `pony.toml`,
  `.env`, Session, or a generic command registry. Child agents do not inherit
  the connection.
- A fine-grained, read-only `GITHUB_PERSONAL_ACCESS_TOKEN` is supplied through
  the current repository's private `.env` or the process environment, with the
  repository value taking precedence. Pony includes it in its secret redaction snapshot;
  the SDK passes it to the MCP child with a restricted inherited environment.
  No token appears in model tool arguments, CLI flags, trace metadata, or
  documentation examples.
- Pony uses the optional Python MCP SDK and a single stdio session per runtime.
  One background event loop bridges the existing synchronous runner contract;
  the SDK owns protocol negotiation and process shutdown. Startup verifies all
  required names and input fields from `tools/list`. Failure closes the client
  and stops before the Agent requests a model response. Call timeouts and
  disconnects produce stable tool errors, with no alternative transport or
  automatic replay.
- The server starts with `--read-only` and exactly
  `get_file_contents,issue_read,pull_request_read`. Pony exposes only its own
  `github_read_file`, `github_read_issue`, and `github_read_pr` specifications.
  Its own validator fixes the selected repository, rejects unexpected keys,
  limits PR/Issue numbers, and blocks sensitive or unsafe remote file paths.
  The three tools have host-owned `read_only` effect classes; MCP annotations
  cannot change Pony permissions.
- MCP `isError` results and SDK protocol-level tool errors are errors. Text and embedded text resources are bounded
  at 4 MiB and passed through Pony's existing redaction, preview, and retained-result flow.
  Binary and unsupported content blocks fail explicitly. Remote text is untrusted data:
  the model may summarize it but it does not gain authority over Pony rules.

## Threat model and limits

The protected assets are the GitHub token, private repository contents, Pony's
local workspace, and its Session/Run artifacts. Model-generated arguments,
remote PR/Issue/file contents, and server responses are untrusted. A malicious
or replaced user-selected executable runs as a Host process with the supplied
token; Pony does not claim to isolate it. The user should install and verify the
official release outside the repository. Host validation narrows accidental
selection of a repository-controlled file but cannot make mutable user-space
binaries immutable or eliminate a check-to-exec race.

The CLI's one-repository restriction limits what model-generated tool arguments
can ask Pony to read. It does not narrow what an over-scoped token could access
if the selected server executable were malicious. A fine-grained token scoped
to the chosen repository is therefore required operationally. GitHub content
returned to Pony may be sent to the configured model Provider; users must
account for that Provider's privacy terms. Known secrets are redacted, but
unknown secrets in remote data cannot be guaranteed absent.

## Evidence and consequences

Offline tests exercise registration, argument validation, Pony's executor,
redaction, errors, and a real SDK stdio round trip against a fake MCP Server.
On Windows, the checksum-verified official GitHub MCP Server v1.12.2 completed
`tools/list` and shutdown using a synthetic token. With a user-provided token,
Pony then read the target repository's `README.md` through the real Server and
`ToolExecutor` in a fresh process; the result was successful and the client closed.
No model Provider request was run, because its credentials are configured separately.

The default Pony installation remains free of the MCP SDK. Installing the
`github-mcp` extra adds the optional dependency. This is a narrow GitHub
adapter, not a general MCP host. Remote write tools and arbitrary MCP Server
configuration require a separate permission/effect design and ADR.
