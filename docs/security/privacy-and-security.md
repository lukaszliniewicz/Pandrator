# Privacy and security

Pandrator stores projects locally. Where their contents are processed depends
on the service selected for each stage and on how the installation is exposed.
Check those choices before processing sensitive documents, media or voice samples.

## Data-flow overview

| Action | Data involved | Where it can go |
| --- | --- | --- |
| Local transcription | Source audio/video processing copy | Configured local transcription service and local storage |
| Local TTS | Speech text, settings, local voice reference | Local TTS service and local storage |
| Cloud LLM correction/translation | Selected subtitle or document batches, instructions, glossary, request metadata | Configured LLM provider |
| DeepL translation | Selected text and request metadata | DeepL |
| Cloud TTS | Speech text, voice/model identifiers, optional reference material | Configured speech provider |
| Passive dispatch | Claimed subtitle batch and context | The model/runtime selected by the MCP host |
| Optional web research | Query terms and bounded source requests | Configured research service and retrieved sites |
| URL import | Source URL and media request metadata | Source service and `yt-dlp`-supported network endpoints |

Local services keep stage data on the host, but backups, operating-system
telemetry, crash collection, and third-party model wrappers have their own
policies. “Local” describes the Pandrator architecture, not every dependency
in existence.

## Credentials

Provider credentials are write-only through normal application APIs and are
redacted from routine diagnostics and job events. Supported storage includes:

- the local Pandrator database;
- Windows Credential Manager, macOS Keychain, or Linux Secret Service through
  the optional credential-store integration;
- owner-restricted secret files where supported;
- environment variables for documented deployments; and
- platform secret stores for containers and pods.

Protect the data directory and use full-disk encryption where appropriate.
Never place tokens in prompts, source artifacts, issue reports, Git, MCP tool
arguments, or non-secret target configuration.

## Network exposure

Local application and recovery interfaces bind to loopback by default. Remote
access is opt-in. Ordinary access requires account authentication; optional
project links provide scoped visitor access. Prefer a VPN or stable
HTTPS reverse proxy. Configure exact public origins, trusted hosts, and the
actual proxy-hop count. Restrict any non-loopback listener with host firewall
or platform network policy.

Plain HTTP is only for an explicitly accepted private-network topology and an
exact restricted CIDR. It is not appropriate for public Internet access.

Pandrator defaults to Solo. An Administrator can explicitly enable named
accounts and shared workspaces for a small team. Each person uses their own
account and each assistant needs its own approved resources/scopes; sharing an
Owner password, local Manager credential or managed MCP bearer does not create
separate user identities. This is a small-team application, not a public
self-registration or untrusted multi-tenant hosting service.

Workspace membership governs team content; instance Administrator status does
not grant blanket access to private content. Workspace Members can edit/process/
export ordinary shared content; Managers additionally govern membership. Only
instance Administrators can acquire missing models or manage accounts. Personal
credentials and libraries do not silently become shared through a project copy.
Read [Solo, accounts and workspaces](../guides/collaboration.md) for setup,
read-only Can view grants, revocation and recovery.

## Optional project links

Public links are disabled by default. An Administrator can enable them in
Application settings. A project owner or workspace Manager can then issue a
time-limited link for project management or translation review. Treat the full
URL as a credential and send it privately.

A management link allows processing with the project's configured resources,
which can incur provider charges. A translation-review link exposes only its
pinned translation and saves proposed corrections; an authorized project editor
must inspect and accept them before they become the selected translation.
Visitors use separate browser credentials and do not sign in as the owner.
See [project links and translation review](../guides/project-links.md) for exact
permissions, revocation and draft recovery.

## MCP boundary

`pandrator-mcp` can run as a Manager-owned loopback Streamable HTTP service or
as a local stdio process beside the agent host. Both are bound to one target.
Tool schemas do not accept origins, proxies, CA paths, tokens, or arbitrary
files. The adapter validates network zone, redirect policy, target identity,
scopes, response bounds, and credential audience before forwarding a bounded
application or Manager operation.

Managed HTTP uses a separate owner-protected bearer file and accepts protocol
requests only on loopback. It validates `Host` and `Origin`; its bearer is not
an application token and is never returned through Manager or MCP tools. Host
configuration containing that bearer is private user configuration. Remote
deployments should keep using stdio beside the agent and a fixed authenticated
Pandrator HTTPS target rather than publishing the loopback MCP service.

Application and app-down Manager recovery use separate audiences and
credentials. The Manager's permanent local bearer remains on the target host.
Use a stable idempotency key when the operation supports one. Before retrying
an uncertain write without a key, inspect whether it already succeeded.
Consequential Manager work uses an immutable reviewed plan.

The host model sees subtitle content when it claims a passive batch. Whether
that model runs locally or sends prompts to a provider is controlled by the
MCP host. Pandrator cannot make a cloud-hosted agent local by calling the
workflow “passive.”

PDF/EPUB parsing and deterministic cleanup remain local. Enabling agentic
document cleanup sends its bounded working text and instructions to the LLM
provider configured in Pandrator; it is not processed by the MCP host's passive
dispatcher. See the
[document-ingestion reference](../reference/document-ingestion.md) for the
stage and artifact boundaries.

Use the least scopes needed. Read-only explanation does not need session or
Manager mutation authority. Exact scope recipes and enrollment behavior are in
the [MCP guide](../../pandrator_mcp/README.md).

## Voice and media rights

A technically usable voice sample is not automatically consented or lawful.
Obtain permission, respect publicity and personality rights, provider/model
terms, and applicable copyright law. Keep provenance with custom voices and
models. The same applies to imported media and URL downloads.

Pandrator does not determine whether your intended cloning, translation,
dubbing, or distribution is lawful.

## Diagnostics and sharing

Manager diagnostics exclude its databases, sessions, credential files, and raw
environment variables, and redact known sensitive fields and local paths.
Third-party libraries and providers can still emit unexpected content.

Before sharing a diagnostic bundle:

1. open it locally;
2. inspect filenames and text logs;
3. remove media, transcript excerpts, paths, or provider identifiers you do
   not intend to disclose;
4. never post a credential or authorization URL; and
5. prefer a private support channel for sensitive reproduction material.

## Operator checklist

- Know which provider receives each document, subtitle, audio, or voice sample.
- Keep local data and credential stores owner-readable.
- Use full-disk encryption and independent backups where appropriate.
- Expose only the application and optional bounded recovery surface.
- Use HTTPS or a VPN and pin remote identities.
- Keep deployment secrets outside images, repositories, and prompts.
- Review agent scopes and plan effects before writes.
- Inspect diagnostics before sharing them.
- Confirm rights and consent for source media and voices.
