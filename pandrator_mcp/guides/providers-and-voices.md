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
3. inspect the voice catalog;
4. verify language and model compatibility; and
5. review the plan's provider disclosures.

Credentials belong in Pandrator's credential store or an approved MCP
credential backend. Never paste a provider key into a tool argument, target
profile, prompt, log, or source artifact.
