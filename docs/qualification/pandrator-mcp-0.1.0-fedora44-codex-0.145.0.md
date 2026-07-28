# Pandrator MCP 0.1.0 — Fedora 44 / Codex 0.145.0 qualification

Date: 2026-07-28/29 (Europe/Warsaw)

Result: **PASS for the initial Codex-controlled trusted-LAN target scope**

This is an implementation qualification record, not a declaration that every
planned `1.0` topology is complete. It exercises one fresh Fedora target from
Codex through the local stdio MCP sidecar. External HTTPS, app-down Manager
recovery, MCP Inspector, and additional agent hosts remain separate gates.

## Systems under test

| Role | System |
|---|---|
| Agent/controller | Codex CLI `0.145.0` on Windows |
| MCP transport | Local stdio, `pandrator-mcp 0.1.0`, MCP SDK `2.0.0` |
| Target | Fedora Linux 44 Workstation, x86_64 |
| Target Python | CPython `3.12.13` (`python3.12-3.12.13-3.fc44`) |
| Application | `pandrator 0.6.0` |
| Media prerequisite | FFmpeg `8.1.2` |
| Network | Fixed trusted-LAN origin over explicitly accepted HTTP |

The controller and target were different machines. The MCP process ran beside
Codex; it did not run on the Fedora target. The target listener was bound to
its LAN address, and a temporary firewalld rule admitted only the controller's
single `/32` address.

## Artifacts

Fresh wheels were built from clean source copies and inspected as ZIP archives:

| Artifact | SHA-256 | Entries | `.pyc` / `__pycache__` |
|---|---|---:|---:|
| `pandrator-0.6.0-py3-none-any.whl` | `45ebc32a7e56e1953f929fcf7e2aedd8409ffcfe2531a70823a6e65331e3d98c` | 288 | 0 |
| `pandrator_mcp-0.1.0-py3-none-any.whl` | `f6f954835a1ba4568ba2bd93c371477c1b6524d3340b0569dd5c62ff2e9625ed` | 63 | 0 |

Fedora received a new Python 3.12 virtual environment and a no-cache wheel
installation. Pandrator and a real worker ran as disposable user services.
The owner password and issued automation credential never appeared in argv,
the MCP target file, Codex configuration, prompts, tool results, or this
record.

After the live run and its resulting fixes, the final working-tree snapshot
was rebuilt again from clean source directories:

| Artifact | SHA-256 | Entries | `.pyc` / `__pycache__` |
|---|---|---:|---:|
| `pandrator-0.6.0-py3-none-any.whl` | `3df51715c1615c3a621340397abaedaff2bcb8b9c6d516239d13b58bd2605e85` | 288 | 0 |
| `pandrator_mcp-0.1.0-py3-none-any.whl` | `5698f82d0ccb80d7a1ce78fe6af4a8b5b223dca35c05d170e85f64f9db61fcb3` | 63 | 0 |
| `pandrator_manager-0.9.0-py3-none-any.whl` | `d51b2c5662898014f4e26daf354bd9646335c9de977447ad053f58600c58be06` | 73 | 0 |

Those post-run artifacts are not presented as the exact Fedora-installed
bits; the first table records those. They passed archive inspection, clean
wheel installation, installed-package path assertions, all three CLI entry
point smokes, and an installed-wheel MCP SDK stdio handshake exposing 28
tools. The final source snapshot also passed `1,021` repository tests, with
`5` platform/optional-feature skips and one upstream Python deprecation
warning. OpenAPI and generated TypeScript regeneration were deterministic;
Svelte reported zero errors and zero warnings, and the production frontend
build completed successfully before the application wheel was rebuilt.

## Codex registration

The stdio server was registered with the supported native shape:

```console
codex mcp add pandrator-fedora-qualification -- \
  /absolute/controller/venv/pandrator-mcp \
  stdio \
  --target fedora-qual \
  --config /absolute/controller/targets.json
```

The registration contained only an executable and a fixed target/config
handle. It contained no downstream origin or credential. The target file held
the exact LAN origin, private CIDR policy, client ID, scopes, credential-store
handle, and pinned instance identity—but never the credential value.

Codex write behavior was also checked. A normal non-interactive run declined
the execution tool when no interactive approval could be collected. The
already reviewed qualification execution proceeded only in a separate
invocation with an explicit operator-selected approval policy. This preserves
the MCP tool's write-risk annotation.

## Scenarios

| Scenario | Result |
|---|---|
| Native browser enrollment with S256 PKCE | Passed |
| Exact origin and stable target-identity pin | Passed |
| Keyring storage without model-visible credential material | Passed |
| Layered MCP doctor: configuration, route, app, API, auth, identity, compatibility, worker | Passed |
| Static explanation plus live system/session facts | Passed |
| Natural guide-topic alias (`durable workflows`) | Passed after schema/alias fix |
| Missing optional Manager scope degrades to a warning | Passed after projection fix |
| Create session with explicit idempotency key | Passed |
| Replay identical session creation and receive the same session | Passed |
| List reusable source and attach it with expected revision | Passed |
| Immutable deterministic `clean_source` plan | Passed |
| Plan reports no external service, provider, cost, or confirmation | Passed after resource-classification fix |
| Execute exact plan ID/digest once | Passed |
| Real worker emits queued, running, progress, and succeeded events | Passed |
| Cleaned artifact registered (47 characters) | Passed |
| Retry exact consumed plan with the original idempotency key | Passed; original work ID returned |
| Re-enroll the same client and rotate the keyring credential | Passed; `credential_rotated: true` |
| Owner CLI lists and revokes the application client | Passed |
| Revoked MCP credential is rejected by doctor | Passed |
| Local logout deletes keyring value and clears enrollment metadata | Passed |

Representative durable handles:

- session: `0f193a6a-b233-4a77-954c-ef6e559a81b4`
- deterministic plan: `74d66a0c-2b74-4060-b402-018c6adfdf99`
- plan digest:
  `d738df3849692dc30fd89714f060d2c49faea802115643fb1da727bfe98a56ee`
- work: `6f319d7e-fd7f-402d-bbe8-b58ec861977a`
- cleaned artifact: `60b11eb6-9294-48cb-8410-3a76f0337a3e`

The lost-response retry returned that same work ID rather than creating a
second job.

## Defects found and corrected

1. Setuptools package data could include stale Python bytecode. Both setuptools
   packages now exclude `.pyc`, `.pyo`, and `__pycache__`, with a regression
   assertion.
2. The guide tool advertised an unconstrained string while accepting only
   canonical topics. Its schema now exposes the topic enum and the registry
   normalizes safe human aliases.
3. `get_system_status(include_manager=true)` failed the entire read when the
   optional Manager scope was absent. It now returns application status plus a
   bounded Manager warning.
4. Deterministic source cleaning incorrectly claimed an LLM resource,
   external-provider disclosure, unknown cost, and confirmations. It now
   claims `service:llm` only when agentic cleaning is enabled.
5. Profile removal could silently leave a native credential behind. Logout,
   explicit remove-time credential policy, rotation reporting, and owner-side
   application/Manager revocation commands were added.
6. Re-login could change the keyring reference and invisibly orphan the old
   value. Reference changes now require logout first.

## Security and scope notes

- The trusted-LAN target used plain HTTP only because both server and client
  explicitly opted into the private-network exception. `doctor` retained a
  warning throughout. HTTPS or a VPN remains the durable recommendation.
- The application was intentionally standalone, so Manager status degraded
  cleanly and direct Manager recovery was not exercised.
- Provider spending, model download, TTS generation, destructive Manager
  operations, and public Internet exposure were outside this run.
- Claude Code, OpenCode, and Antigravity configuration generators were not run
  as live hosts. They remain syntax-tested, secret-free templates rather than
  compatibility claims.

## Cleanup

Cleanup was verified:

- the application automation client was revoked;
- the controller keyring value and enrollment metadata were removed;
- the Codex MCP registration was removed;
- both transient Fedora user services were stopped;
- the temporary firewalld rule was removed; and
- the disposable Fedora qualification data/venv directory was deleted.

The parallel Fedora `python3.12` and `python3.12-devel` RPMs were deliberately
left installed; they do not replace Fedora's system Python.

## Remaining release gates

Before calling `pandrator-mcp 1.0` broadly qualified:

1. run external HTTPS, certificate, proxy, and DNS-rebinding cases;
2. run a managed installation with app-down Manager recovery;
3. run the MCP Inspector release smoke (stdio framing is covered);
4. run the secret, dependency, and SBOM gates; and
5. publish a release record for the exact distributable artifacts.
