# Connect an agent to Pandrator

Pandrator supports two MCP connection shapes. Use the managed HTTP service when
the agent and Pandrator run for the same local user. Use stdio when the MCP
process must run beside an agent on another workstation, when a host has no
Streamable HTTP support, or when you need an explicitly named remote target.

| Situation | Recommended transport | Why |
| --- | --- | --- |
| Pandrator and agent on the same computer | Manager-owned Streamable HTTP | One persistent service; the Manager starts, monitors, and reports it |
| Agent on another computer | Local stdio beside the agent | Local file access belongs to the agent computer; only bounded API traffic crosses the network |
| Older MCP host | Stdio | Preserves the maintained initialization and process transport |
| Headless Pandrator server | Stdio on the agent workstation to a fixed HTTPS target | Does not expose a second public automation listener |

## Managed HTTP on the same computer

An application bundle with Pandrator's automation extra registers the optional
`pandrator.mcp` service. The Manager starts it after the application and worker,
monitors `GET /health`, and reports its state in `GET /v1/application` and
`GET /v1/services`. An MCP failure is visible there and in the service log, but
does not prevent the browser application from starting.

The service listens only on `127.0.0.1:8099`. Its MCP endpoint is:

```text
http://127.0.0.1:8099/mcp
```

Every protocol request requires the separate bearer stored at
`<workspace>/Pandrator/state/mcp.secret`. The Manager creates that file with
owner-only protection. It is not the Manager client credential or an
application token, and it must not be placed in a prompt, repository, project
configuration, or MCP tool argument.

Generate a host fragment only from a private local terminal. The explicit flag
acknowledges that the output contains the bearer:

```console
pandrator-manager --workspace /path/to/parent mcp-config codex --include-credential
pandrator-manager --workspace /path/to/parent mcp-config opencode --include-credential
pandrator-manager --workspace /path/to/parent mcp-config claude-code --include-credential
pandrator-manager --workspace /path/to/parent mcp-config antigravity --include-credential
```

The workspace is the parent selected in the launcher, not its inner
`Pandrator` directory. Merge the generated entry into the host's private user
configuration and keep that file owner-readable. Do not paste the fragment
into chat or commit it.

The managed service uses a non-secret target named `managed-local` in
`<workspace>/Pandrator/state/mcp-targets.json`. New installations expose the
current user's home directory as source root `home` and use
`<workspace>/exports` as the output root. These paths can be changed from the
application Settings page or through the Manager CLI:

```console
pandrator-manager --workspace /path/to/parent mcp-paths source-add \
  downloads /home/me/Downloads
pandrator-manager --workspace /path/to/parent mcp-paths output-set \
  /home/me/Pandrator-outputs
pandrator-manager --workspace /path/to/parent mcp-paths list
```

On Windows, use absolute Windows paths and the corresponding workspace path.
The model sees only the approved root names and relative entries. Managed path
changes are reloaded on the next browse/import/download operation, so an
application restart is unnecessary. The lower-level `pandrator-mcp target`
commands remain available to development and custom installations.

## Stdio and remote targets

Stdio starts one MCP process for one named target. Its generated host fragment
contains an executable, target name, and non-secret configuration path; the
application and optional recovery credentials stay in their configured local
credential stores.

```console
pandrator-mcp host-config codex --target production
pandrator-mcp host-config opencode --target production
```

For a remote Pandrator installation, run this process on the agent workstation.
That placement lets the agent import from directories explicitly approved on
its own machine while Pandrator remains behind one authenticated HTTPS API.
Do not publish the loopback managed HTTP listener through a reverse proxy. A
future remote HTTP mode requires its own audience-bound OAuth contract; an
application token is deliberately not accepted as an MCP bearer.

## Protocol and security behavior

The managed HTTP endpoint uses the final MCP 2026-07-28 stateless request
model. It validates `Host` and `Origin`, limits request bodies, rejects duplicate
or invalid authorization headers, and binds to a loopback IP rather than a
wildcard interface. Stdio keeps protocol frames on stdout and redirects
dependency diagnostics to stderr.

Both transports expose the same tools, resources, prompts, typed failures, and
Pandrator plans. Changing transport does not expand the agent's application or
Manager authority.

For exact target enrollment, scope recipes, host fragments, and diagnostics,
continue with the [Pandrator MCP guide](../../pandrator_mcp/README.md). For
networked installations, see [remote and headless operation](remote-and-headless.md)
and [privacy and security](../security/privacy-and-security.md).
