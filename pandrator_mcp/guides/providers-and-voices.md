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

## Primary and compatibility providers

The TTS catalogue recommends `audio_cpp` for new local work. XTTS, Silero,
Kokoro, and Voxtral remain dedicated local providers. The default catalogue
omits standalone Qwen3 TTS, Fish S2 Pro, VoxCPM2, Chatterbox, and Magpie
compatibility entries. Use `include_compatibility=true` to inspect them all,
or supply a saved `service_id` to retrieve that provider directly.

Inspect `catalogue_role`, `replacement_service_id`, and
`replacement_model_family` instead of maintaining your own retirement list.
An existing session may deliberately use a compatibility provider; inspect its
settings before selecting a replacement. To switch, inspect the target's live
models and voices, then call `pandrator_configure_tts` with their exact IDs.
For audio.cpp cloning models, supply a voice with a ready managed reference
link. Changing provider clears stale aliases, reference settings, and old
engine options; it does not convert old model IDs or uploaded voices. Existing
takes and installed engines are retained.

## Expressive capabilities and context

Request `pandrator_get_tts_catalog` with full detail to inspect each model's
`expressive_capabilities`. Instruction support is model/variant/backend-specific:
voice design is not the same as directing a fixed voice. The catalogue distinguishes
separate instructions, inline directions, span approximations, known event
spellings and prompt-separated semantic context. Unknown routes must not inherit
capabilities merely because another model in the family supports them.

Pandrator compiles `pandrator.performance/v1` annotations into Fish S2 inline
controls, capable Qwen/OpenAI instruction fields, Gemini's labelled prompt, Eleven v3 audio tags, and
supported event-only formats. Never put native provider tags into the stored
transcript. Preview an actual block with `pandrator_preview_performance_plan` to
see applied, approximate, unsupported and disabled controls without synthesizing.
"Applied" is not a guarantee of acoustic compliance. Non-pause vocalizations are
opt-in and default off.

Gemini's `generation_prompt` can be combined with `tts_context_mode=before` or
`both`; `performance_context_before`, `performance_context_after` and
`performance_context_max_chars` bound the read-only context. Other-speaker text
is labelled semantic evidence, not a new voice reference. For backends lacking
unspoken text context, use a contextual performance plan to derive supported
utterance directions instead. Qwen Base/cloning is not instruction-capable on
the reviewed route. Do not substitute previous generated audio for semantic
context automatically.

Credentials belong in Pandrator's credential store or an approved MCP
credential backend. Never paste a provider key into a tool argument, target
profile, prompt, log, or source artifact.

For searchable profiles, collections, voice design, reference import and passive
multi-voice casting, read the `voice-casting` guide. Start with
`pandrator_get_voice_capabilities` and `pandrator_get_voice_catalog`; use
`generation-controls` for the XML and character dictionary contract.

## Configure directions and context in one operation

`pandrator_configure_tts` can atomically set `style_instructions`,
`tts_context_mode` (`off`, `before`, or `both`), `performance_context_before`,
`performance_context_after`, `performance_context_max_chars`, and
`performance_allow_vocalizations` along with the exact service/model/voice.
Inspect the full catalogue first: unsupported or unknown requested controls are
rejected before saving. Omitted options keep existing values; an empty style
clears it. Configuration does not analyse, adopt a performance plan, or generate
speech.

For Gemini, configure `tts_context_mode="both"`, the desired neighbour counts,
and a context budget in the same call as `style_instructions`. This automates
context setup; Pandrator derives each request's neighbours from the accepted
speech plan. Keep scene/language boundaries and inspect a compiled preview.

For ElevenLabs, `eleven_v3` supports inline direction and mapped vocal tags but
not preceding/following stitching fields. Supported v2/v2.5 models support those
native context fields but not v3 expressive tags. Never transfer controls by
provider name alone. Optional `elevenlabs_voice_settings` replaces the settings
object; `{}` restores provider defaults. Native REST requests accept
stability/similarity_boost/style 0–1, speed 0.25–4 and boolean
use_speaker_boost. The helper validates these before saving. Provider documentation
differs on non-stability settings for v3; they are forwarded but reported as
approximate and may be ignored. Existing Turbo v2.5
selections remain discoverable; upstream recommends Flash v2.5 for new work.

For direct whole-block or phrase edits use `pandrator_preview_speech_selection`
and `pandrator_apply_speech_selection`, following the `generation-controls`
guide. `pandrator_update_generation_segment` changes text, voice, or language;
it is not a delivery-instruction editor.
