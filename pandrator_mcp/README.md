# Pandrator MCP

`pandrator-mcp` is a local stdio sidecar for explaining Pandrator, inspecting
an instance, and carrying out bounded application and Manager actions. It can
control Pandrator on the same computer, a trusted LAN or VPN, an external
HTTPS server, or a pod.

The sidecar talks only to Pandrator's versioned HTTP APIs. It does not import
the application ORM or job queue, accept model-selected URLs, expose arbitrary
files or commands, or store credentials in MCP arguments and results.

For public workflow concepts, start with
[passive dispatch](https://github.com/lukaszliniewicz/Pandrator/blob/main/docs/guides/passive-dispatch.md),
[remote and headless operation](https://github.com/lukaszliniewicz/Pandrator/blob/main/docs/operations/remote-and-headless.md),
and [privacy and security](https://github.com/lukaszliniewicz/Pandrator/blob/main/docs/security/privacy-and-security.md).
This component guide remains the canonical source for exact MCP installation,
target, scope, enrollment, host-configuration, diagnostic, and protocol
behavior.

## What agents can do

The current tool surface supports:

- packaged, versioned explanations of Pandrator and its workflows;
- target, capability, session, workflow, source, artifact, provider, voice,
  durable-work, and redacted-event inspection;
- safe browsing of operator-approved local roots, resumable source import, and
  verified artifact download without putting file bytes in model context;
- session creation and revision-safe session, source, and settings changes;
- catalog-backed TTS service, model, and voice selection plus typed export
  variants and generation-run selection;
- immutable workflow planning followed by explicit execution;
- passive subtitle correction/translation, PDF/EPUB source-cleaning, and
  speech-text optimization runs with sequential pull, short-lived batch
  leases, and typed submissions;
- durable-work cancellation;
- Manager status and diagnostics;
- immutable Manager component plans, runtime control, and plan execution; and
- local-managed and separately enrolled HTTPS recovery access when Pandrator
  itself is unavailable.

Complex actions stay in Pandrator. An agent first obtains a native immutable
plan, presents its effects and confirmation requirements, and then executes
that exact plan. Every write uses an idempotency key. Concurrent changes
produce an explicit revision or stale-plan error instead of a silent
overwrite.

For document workflows, the public
[document-ingestion reference](https://github.com/lukaszliniewicz/Pandrator/blob/main/docs/reference/document-ingestion.md) explains
PDF layout/OCR, EPUB structure, deterministic and optional model-assisted
cleanup, artifacts, and narration preparation. MCP plans and monitors those
native stages; it does not parse books in the sidecar. Source cleaning can use
Pandrator's configured provider or the passive dispatcher, where the MCP host
model performs editorial review and Pandrator retains parsing, validation, and
artifact ownership.

### Subtitle dispatch

Dispatch is a passive pull loop for correction and translation. Create a run,
then claim one batch at a time. Only the claim response discloses that batch's
canonical `task`, actionable `batch.cues`, bounded boundary context, and
short-lived `lease_token`; list/get calls return metadata only. Cue text occurs
once. Timing is nested once per actionable cue in `full` mode, limited to
positive overlap in `overlap_only`, and absent in `none` mode. Stable
source-revision `cue_id` values are used for submission; `batch_ordinal` is
presentation only. Claim reports `run_status` and `batch_status` separately so
replaying a completed claim cannot masquerade as a completed run.
An explicit translation artifact may also be the source of a correction run;
the returned `output_role: translation` preserves its language and downstream
track semantics while a new translation revision is materialized.

Submit a typed `result`: correction uses `kind: correction` plus operations
over `cue_ids`; translation uses `kind: translation`, one item per `cue_id`,
and optional `glossary_updates`. `response_text` remains only as a compatibility
path for raw model adapters. Accepted batches are followed by the next
sequential claim. If validation rejects a result, repair and resubmit it while
the lease remains valid. Renew slow work or release abandoned work. The final
accepted batch automatically finalizes the run; retry the same final submission
and idempotency key if transient materialization leaves it `finalizing`.

### PDF and EPUB source-cleaning dispatch

For an attached PDF or EPUB, call
`pandrator_create_source_cleaning_dispatch_run`. Preparation is asynchronous
because local PDF extraction may include OCR. Inspect the run until it is
`ready`, then claim and submit six sequential phases with the corresponding
source-cleaning tools.

A claim discloses only that phase's document summary, bounded evidence,
candidate blocks, server proposals, and operation allowlist. Decide every
proposal with `accept` or `reject`; optionally add phase-appropriate typed
operations over disclosed block IDs. Do not return rewritten book text.

The claim's candidates are a starting point, not a heuristic gate. While the
lease is active, use
`pandrator_inspect_source_cleaning_dispatch_extraction` to browse, search,
inspect context or structure, preview selectors, and batch independent
lookups. Use `view: working` for the result of accepted earlier phases and
`view: baseline` for the original deterministic extraction. EPUB runs also
offer a read-only `view: source` over the structured publisher markup, so a
host model can investigate doubtful omissions, links, IDs, and formatting.
Working/baseline IDs returned by inspection are added to the batch's audited
valid scope; source-only IDs are reported separately and can never be mutation
targets. The final `text_repair` phase permits targeted `replace_block`
corrections for confirmed extraction defects.

Footnote inspection returns structurally supported candidates by default.
Plain numbered lines are deliberately withheld because captions, lists, and
numbered prose are common; request `include_ambiguous: true` to inspect those
low-confidence lines when the edition warrants a broader search.

There is no provider, model-token, or iteration budget in this passive path.
`evidence_limit` is only a per-phase transport bound: default 500, range
20–2,000. MCP application responses permit 8 MiB by default with a 16 MiB
safety ceiling. After the last phase, Pandrator applies and validates the
accepted operations and selects the resulting `clean_text` artifact. Normal
workflow planning can then continue with narration preparation.

The source must already be managed and attached. Use
`pandrator_import_local_source` for a file beneath an operator-approved named
root, or attach a source returned by `pandrator_list_sources`. The cleanup
tools themselves never accept an arbitrary filesystem path.

### Speech-optimization dispatch

After narration preparation or subtitle translation/correction, call
`pandrator_create_speech_optimization_dispatch_run` to let the MCP host model
prepare text for speech without a Pandrator LLM provider. Sources are managed
SRT, prepared-narration JSON, or TXT artifacts; PDF/EPUB and media inputs must
first pass through extraction or transcription.

Claim batches sequentially. The claim's `batch.units` are the only actionable
items and carry stable `unit_id` values, language, optional speaker, and—when
requested—one timing object per SRT unit. Boundary context is read-only and
never repeats timing. Submit `kind: speech_optimization` with every `unit_id`
exactly once, in order, leaving text unchanged when no improvement is needed.

`char_limit` and `max_units_per_batch` are transport bounds rather than model
budgets. Defaults are 20,000 characters and 100 units; supported maxima are
1,000,000 and 500. Pandrator neither chooses nor calls a model, and the host may
subdivide a claimed batch internally. The final accepted batch materializes a
normal `tts_optimized` artifact, pinned against source, selection, and output
changes. Finish this stage before starting speech generation.

Guidance remains available even when no target can be reached.

### MCP protocol compatibility

Version 0.2.0 pins the official Python SDK 2.1.1 and supports the final
[MCP 2026-07-28 specification](https://modelcontextprotocol.io/specification/2026-07-28).
Modern hosts connect through `server/discover` and attach the negotiated
protocol metadata to requests; maintained older hosts can still use the legacy
`initialize` handshake. The same server therefore works with current clients
without abruptly abandoning existing integrations.

Tools expose JSON Schema inputs, annotations, structured JSON results, and a
text JSON fallback. Tool, resource, and prompt discovery are deterministic.
Expected business failures are returned as normal MCP tool results with
`isError: true` and a typed Pandrator error object in the text. Modern list
results include private-cache hints and every ordinary result carries the
required `resultType` through the SDK.

For stdio, stdout is reserved exclusively for newline-delimited MCP frames;
dependency diagnostics from both tool and resource handlers are redirected to
stderr. Protocol discovery, structured-content fallback, modern stdio, legacy
negotiation, annotations, and stdout-noise containment are covered by the
contract tests.

The server deliberately does not advertise deprecated protocol Roots,
Sampling, or Logging capabilities. Its named local roots are ordinary sidecar
configuration, not the deprecated MCP Roots feature. Pandrator's durable work
and passive dispatch handles remain explicit tool arguments rather than using
the optional Tasks extension; this keeps long-running application state usable
by both modern and maintained legacy hosts.

## Install

The current release is 0.2.0 and requires Pandrator 0.8.16 or newer. With
Python 3.11 or 3.12, install it as an
isolated command-line tool:

```console
pipx install "pandrator-mcp[credential-stores,manager]"
# or
uv tool install "pandrator-mcp[credential-stores,manager]"
```

For development from the repository root:

```console
python -m pip install -e "./pandrator_mcp[credential-stores,manager]"
```

The `credential-stores` extra is recommended because the enrollment flow can
then save tokens in Windows Credential Manager, macOS Keychain, or Linux
Secret Service. The `manager` extra supports local Manager discovery; you may
omit it when the sidecar will use only external targets.

For a local Manager installation:

```console
pandrator-mcp target add local --mode local --workspace C:\Pandrator
pandrator-mcp target pin local
pandrator-mcp doctor --target local
pandrator-mcp stdio --target local
```

On Linux, the workspace could instead be `/srv/pandrator`. A process is bound
to one named target at startup; tools cannot switch its destination.

## Target configuration

The default non-secret target file is:

- `%APPDATA%\Pandrator\mcp-targets.json` on Windows; or
- `$XDG_CONFIG_HOME/pandrator/mcp-targets.json` on Linux.

Use `--config PATH` or `PANDRATOR_MCP_CONFIG` to select another file.
`pandrator-mcp print-config` prints a public projection without credential
references.

### Approved local files and outputs

The operator—not the model—chooses which sidecar-host directories are visible.
Expose each source directory under an opaque name and configure one output
directory:

```console
pandrator-mcp target source-root-add local downloads /home/me/Downloads
pandrator-mcp target source-root-add local library /mnt/storage/14-Library
pandrator-mcp target output-root-set local /home/me/Pandrator-outputs
pandrator-mcp target source-root-list local
```

Use Windows absolute paths when the sidecar runs on Windows. The MCP model sees
root names and relative entries, never these absolute paths. Symlinks are not
offered for import, and the importer opens every path component without
following symlinks.

`pandrator_browse_local_sources` lists approved entries.
`pandrator_import_local_source` hashes the selected regular file, reuses an
identical managed source when possible, otherwise uploads it in resumable
chunks, and then attaches it with the inspected session revision. Upload state
and completion results are replayable, so an interrupted model turn can safely
continue.

“Local” here means local to the stdio sidecar. Pandrator itself may be on the
same computer or at a fixed remote target. For a remote target the sidecar
streams bytes directly over the authenticated Pandrator API; the bytes are
never encoded into an MCP tool result or model prompt.

`pandrator_download_artifact` performs the reverse operation. It streams one
immutable artifact into the approved output root, resumes a partial transfer
with HTTP Range, verifies size and SHA-256 metadata, and atomically publishes
the completed file. It never returns the remote server's storage path.

### End-to-end agent workflow

For a request such as “take the course video in Downloads, transcribe it,
correct and translate the subtitles, generate a voiceover, and give me two
final variants,” the intended sequence is:

1. call `pandrator_recommend_next_steps`, target status, and the matching guide;
2. browse an approved root, create or inspect a session, and import the source;
3. plan and execute transcription, then poll the returned work to terminal;
4. run passive correction and translation sequentially, with the host model
   producing and submitting each batch itself;
5. optionally run passive speech optimization before generation;
6. resolve live TTS choices with `pandrator_get_tts_catalog`, inspect the TTS
   settings revision, and apply exact IDs with `pandrator_configure_tts`;
7. plan and execute generation, poll it to terminal, and inspect generation
   runs;
8. create and execute one `pandrator_plan_export_variant` per requested output;
   then
9. list the resulting artifacts and download each requested deliverable.

Service, model, and voice names mentioned by a user are preferences or
examples. The agent must match them against the live catalog and ask for a
choice only when the requested option is absent or ambiguous. Upload chunking,
download resumption, and retry transport details are automatic rather than
model-facing knobs.

### Scope recipes

Request only the authority the agent needs:

| Purpose | Application enrollment scopes |
|---|---|
| Explain and inspect | `app.read` |
| Create and edit sessions | `app.read`, `app.write` |
| Import approved local files or configure TTS | `app.read`, `app.write` |
| Run and cancel workflows | `app.read`, `app.write`, `app.run`, `app.cancel` |
| Complete media-to-deliverables workflow | `app.read`, `app.write`, `app.run` (add `app.cancel` if cancellation is required) |
| Correct or translate subtitles through dispatch | `app.read`, `app.run` |
| Clean an attached PDF or EPUB through passive dispatch | `app.read`, `app.run` |
| Optimize prepared speech text through passive dispatch | `app.read`, `app.run` |
| Inspect Manager through Pandrator | add `manager.read` |
| Start or stop managed runtimes | add `manager.runtime` |
| Execute reviewed Manager plans | add `manager.mutate` |

`app.credentials.read` and `app.credentials.write` exist in the application
authorization model, but the MCP does not expose credential values or general
credential-setting tools.

Direct app-down recovery is a different audience. Its possible scopes are
`manager.read`, `manager.runtime`, and `manager.mutate`, and it requires a
separate enrollment.

### External HTTPS server

This is the recommended remote topology: the agent host runs the local stdio
sidecar and the sidecar connects to a fixed Pandrator HTTPS origin.

```console
pandrator-mcp target add production ^
  --mode external ^
  --origin https://pandrator.example ^
  --scope app.read ^
  --scope app.write ^
  --scope app.run ^
  --scope app.cancel ^
  --scope manager.read

pandrator-mcp target login production
pandrator-mcp target pin production
pandrator-mcp doctor --target production
```

Use `\` instead of `^` for POSIX shell line continuation. `target login`
opens an out-of-band owner-consent page, uses S256 PKCE, and writes the issued
token directly to the OS credential store. `--headless` provides the
copy/paste TTY flow, while `--no-open-browser` prints the authorization URL.
Tokens are never accepted on the command line.

An identity pin captures the stable application instance ID and canonical
origin. A changed identity fails closed. Only use
`target pin --replace-identity` after independently verifying an intentional
rebuild.

### Home, LAN, or VPN

Prefer HTTPS or a VPN. For a private certificate authority:

```console
pandrator-mcp target add home ^
  --mode lan ^
  --origin https://pandrator.home.arpa ^
  --allowed-cidr 192.168.10.0/24 ^
  --ca-bundle C:\certificates\home-ca.pem ^
  --scope app.read ^
  --scope app.write ^
  --scope app.run ^
  --scope app.cancel

pandrator-mcp target login home
pandrator-mcp target pin home
pandrator-mcp doctor --target home
```

Every DNS result must remain inside an explicitly configured private CIDR.
Link-local and cloud-metadata destinations remain forbidden. Deliberately
accepted private HTTP additionally requires `--allow-insecure-http`; it is
not available for Internet targets or direct Manager recovery.

### Application without Pandrator Manager

Use `--mode external-application` when an external deployment intentionally
has no Manager:

```console
pandrator-mcp target add hosted-app ^
  --mode external-application ^
  --origin https://pandrator.example ^
  --scope app.read ^
  --scope app.write
```

Application tools continue to work. Manager tools return the typed
`manager_unavailable` result instead of inventing a control plane.

## Optional app-down Manager recovery

Normal Manager calls go through Pandrator's same-origin, bounded Manager
proxy. The Manager's permanent local bearer stays on the target host.

For recovery while Pandrator is stopped, prepare a distinct HTTPS Manager
origin and add it to the target:

```console
pandrator-mcp target configure-recovery production ^
  --origin https://recovery.pandrator.example ^
  --recovery-scope manager.read ^
  --recovery-scope manager.runtime

pandrator-mcp target login production --manager-recovery
pandrator-mcp doctor --target production
```

`configure-recovery` preserves the application origin, scopes, enrollment,
and identity. By default it also reuses the target's non-secret automation
client ID for attribution across the two audiences. It refuses to overwrite
an already enrolled recovery credential; revoke that client before changing
its recovery identity.

Recovery enrollment requires an already authorized Manager recovery browser.
The approval page shows the client, application and recovery identities,
requested scopes, and expiry. The resulting credential:

- is accepted only by that Manager recovery audience;
- is bound to the application origin and both instance identities;
- expires after at most 30 days;
- is rate-limited per automation client;
- cannot access network settings, arbitrary files, or the permanent bearer;
  and
- is audited with client, request, and trace identifiers.

Availability failures may fall back from the application proxy to enrolled
recovery. Authorization, scope, identity, and policy failures never do.

Remote Manager mutations through the application proxy also require the
operator to set `PANDRATOR_ALLOW_REMOTE_MANAGER_MUTATIONS=1` on that
single-owner deployment. Possessing an application or recovery token does not
enable that server-side policy.

## Preparing a home server or pod

The Manager's documented remote launcher prepares the two canonical origins:

```console
pandrator-manager-launcher setup ^
  --workspace /srv/pandrator ^
  --remote-setup-url https://recovery.pandrator.example ^
  --remote-pandrator-url https://pandrator.example ^
  --trusted-proxy-hops 1 ^
  --no-open
```

For an ingress in another pod or network namespace, also pass
`--network-bind-host 0.0.0.0` and restrict that listener with platform network
policy. A production deployment should:

1. assign stable DNS names and valid HTTPS certificates;
2. persist the Pandrator data root and Manager workspace/state;
3. configure the exact public origins and trusted proxy-hop count;
4. expose only Pandrator and, if needed, the bounded recovery origin;
5. keep the permanent Manager client endpoint local;
6. put deployment secrets in the platform secret store;
7. enroll and pin from the workstation running the agent host; and
8. pass `doctor` before enabling writes.

### Fedora standalone smoke

For a disposable Fedora workstation/server smoke without Manager, install a
parallel supported Python and create a dedicated data root:

```bash
sudo dnf install python3.12 python3.12-devel
python3.12 -m venv ~/.local/share/pandrator/venv
~/.local/share/pandrator/venv/bin/pip install /path/to/pandrator-VERSION-py3-none-any.whl
ffmpeg -version

export PANDRATOR_DATA_DIR="$HOME/.local/share/pandrator/data"
~/.local/share/pandrator/venv/bin/pandrator auth init
~/.local/share/pandrator/venv/bin/pandrator serve \
  --host SERVER_LAN_IP \
  --port 8097 \
  --public-url http://SERVER_LAN_IP:8097 \
  --allow-insecure-remote \
  --no-open-browser
```

Run `pandrator worker` with the same `PANDRATOR_DATA_DIR` in a second
terminal or user service. Restrict the listener to the controller address;
for example, a temporary firewalld rule can use:

```bash
sudo firewall-cmd --add-rich-rule='rule family="ipv4" source address="CONTROLLER_IP/32" port port="8097" protocol="tcp" accept'
```

This deliberately accepted HTTP layout is suitable only for a trusted LAN
smoke. Use HTTPS or a VPN for a durable home/server installation, and create
reviewable user-service units instead of relying on terminal processes.

Losing the application or Manager state volume changes its stable identity.
The MCP rejects the replacement until the owner verifies and deliberately
re-enrolls it.

Codex, Antigravity, OpenCode, or Claude Code can help prepare a pod, service
unit, reverse proxy, TLS ingress, persistent volumes, and firewall rules. A
useful request is:

```text
Help me prepare a single-owner Pandrator pod on <platform>.
Use https://pandrator.example for the application and
https://recovery.pandrator.example for optional recovery. Follow the
Pandrator MCP remote-target guide, persist application and Manager state,
restrict ingress, and put credentials only in the platform secret store.
Generate reviewable infrastructure code and validation commands. Stop before
deploying or exposing the service, show me the proposed network surface, and
ask for approval.
```

Review generated infrastructure as carefully as application code. The agent
can accelerate setup, but the operator still owns DNS, certificate, firewall,
volume, and exposure decisions.

## Configure an MCP host

Generate a secret-free local-stdio fragment after the target exists:

```console
pandrator-mcp host-config codex --target production
pandrator-mcp host-config claude-code --target production
pandrator-mcp host-config opencode --target production
pandrator-mcp host-config antigravity --target production
```

The generated command is always:

```text
pandrator-mcp stdio --target production --config <absolute-target-file>
```

Use `--executable ABSOLUTE_PATH` if the host does not inherit the shell's
`PATH`, and `--server-name NAME` to change the host-visible name.

| Host | Generated format | Typical project location |
|---|---|---|
| Codex | `[mcp_servers."…"]` TOML | `.codex/config.toml` |
| Claude Code | stdio `mcpServers` JSON | `.mcp.json` |
| OpenCode V2 | `mcp.servers` local-command JSON | `opencode.json` |
| Antigravity | stdio `mcpServers` JSON | `.agents/mcp_config.json` |

Merge the fragment into an existing host file rather than overwriting
unrelated servers. Give each Pandrator target a separate entry. Never add an
origin, token, proxy, certificate path, or credential reference to model-
visible tool arguments.

The generated fragments contain commands and non-secret file paths, never
tokens. You need to configure only the agent host you actually use.

For Codex, either merge the generated TOML or register the exact stdio command:

```console
codex mcp add pandrator-production -- pandrator-mcp stdio --target production --config /absolute/path/to/mcp-targets.json
codex mcp get pandrator-production
```

The generated Codex fragment uses `default_tools_approval_mode = "writes"`.
Interactive writes therefore remain reviewable. A non-interactive `codex
exec` run rejects an approval-requiring write unless the operator explicitly
selects an appropriate approval policy for that already reviewed action.

## Diagnostics and lifecycle

Run these before giving an agent write authority:

```console
pandrator-mcp target list
pandrator-mcp target test production
pandrator-mcp doctor --target production
pandrator-mcp print-config
```

`doctor` checks profile validation, DNS/network policy, TLS, API
compatibility, authentication, pinned identity, Manager and recovery state,
and capabilities. It never prints a token or credential reference.

Server-side revocation and local removal are distinct:

- rotate an application credential by running `target login NAME` again; the
  same client ID is retained, the old server token is revoked, and the native
  keyring value is replaced;
- list and revoke application clients on the target host with
  `pandrator --data-dir DATA --json auth automation-client list` and
  `pandrator --data-dir DATA --json auth automation-client revoke CLIENT_ID
  --yes`;
- list and revoke recovery clients as the local Manager owner with
  `pandrator-manager --workspace WORKSPACE automation-client list` and
  `pandrator-manager --workspace WORKSPACE automation-client revoke CLIENT_ID
  --yes`;
- delete the workstation keyring credential with `pandrator-mcp target logout
  NAME --yes` (add `--manager-recovery` for that separate audience); then
- remove the non-secret profile with `pandrator-mcp target remove NAME --yes`.

Removing a target does not delete remote Pandrator data and is not a
substitute for server-side credential revocation. If a profile still
references credentials, removal refuses to guess: choose
`--delete-local-credentials` or `--keep-local-credentials` explicitly. Both
options still leave server-side revocation to the owner commands above.
Changing a credential-store reference during re-login is also refused until
logout, preventing an old native secret from becoming an invisible orphan.

## Security boundaries

- The sidecar connects only to the fixed target saved in its local profile;
  tools cannot supply a different destination.
- Tokens are resolved from the credential store and are never accepted in MCP
  arguments.
- Inherited HTTP proxy settings, redirects, and model-supplied URLs are
  ignored or rejected.
- Internet targets require HTTPS and public DNS addresses. LAN targets must
  stay inside the private networks listed in their profile.
- Direct Manager recovery always uses authenticated HTTPS.
- Pandrator application access and Manager recovery use separate credentials
  that can be revoked independently.
- A changed server identity fails closed until the owner verifies and pins the
  replacement.
- Work and event tools return bounded, redacted projections rather than raw
  job payloads or secrets.

Agents can read the packaged `pandrator://guide/security-boundaries` guide for
the same rules while they work.
