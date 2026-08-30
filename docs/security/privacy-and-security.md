# Privacy and security

Pandrator is local-first, not magically private under every configuration.
Where data goes depends on the service selected for each stage and on how the
installation is exposed. Review those boundaries before processing sensitive
documents, media, or voice samples.

## Data-flow overview

| Action | Data involved | Where it can go |
| --- | --- | --- |
| Local transcription | Source audio/video processing copy | Local CrispASR service and local storage |
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
access is opt-in and requires owner authentication. Prefer a VPN or stable
HTTPS reverse proxy. Configure exact public origins, trusted hosts, and the
actual proxy-hop count. Restrict any non-loopback listener with host firewall
or platform network policy.

Plain HTTP is only for an explicitly accepted private-network topology and an
exact restricted CIDR. It is not appropriate for public Internet access.

Pandrator is a single-owner system. Do not expose it as a general public
multi-user service or treat owner credentials as tenant isolation.

## MCP boundary

`pandrator-mcp` runs as a local stdio sidecar beside the agent host and is bound
to one named target at startup. Tool schemas do not accept origins, proxies,
CA paths, tokens, or arbitrary files. The sidecar validates network zone,
redirect policy, target identity, scopes, response bounds, and credential
audience before forwarding a bounded application or Manager operation.

Application and app-down Manager recovery use separate audiences and
credentials. The Manager's permanent local bearer remains on the target host.
Every mutation uses an idempotency key; consequential Manager work is based on
an immutable reviewed plan.

The host model sees subtitle content when it claims a passive batch. Whether
that model runs locally or sends prompts to a provider is controlled by the
MCP host. Pandrator cannot make a cloud-hosted agent local by calling the
workflow “passive.”

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
