# Providers, models, and voices

Providers describe configured LLM, translation, speech-recognition, and
text-to-speech services. Provider status exposes readiness and whether a
credential is configured, never the credential value or credential reference.
Some providers are local; others transfer text, audio, or metadata outside the
Pandrator host.

The voice catalog contains reusable voice identities and optional provider
bindings. Voice samples and transcripts are separate artifacts and are not
returned by the catalog tool. A voice can be suitable for one language or
provider without being available to every configured TTS service.

Before planning generation:

1. inspect capabilities;
2. inspect provider status;
3. call `pandrator_get_tts_catalog`, refreshing it when current readiness or a
   dynamic catalog matters;
4. inspect the managed voice catalog and its ready provider registrations;
5. verify language, model, and voice compatibility;
6. inspect the session's current `tts` settings revision;
7. call `pandrator_configure_tts` with exact advertised IDs; and
8. review the generation plan's provider disclosures.

Names in a user's request may be examples, display labels, or stale catalog
values. Match case-insensitively only after the catalog has supplied a unique
canonical service/model/voice value. A managed voice is usable only when its
registration for the chosen service is `ready`; send the provider's registered
voice ID, not Pandrator's display name. Ask the user when a materially different
substitution would be required.

Credentials belong in Pandrator's credential store or an approved MCP
credential backend. Never paste a provider key into a tool argument, target
profile, prompt, log, or source artifact.
