# Model identities and speech controls

A model can offer different controls depending on how it is served. Use the
model catalogue and selection preview to check the route you have configured.
The preview shows how Pandrator will send your directions; a model card can
describe features that the selected adapter does not provide.

## Catalogue identities

Standalone models have `entry_role: model`. AuK's Qwen conditioning weights and
VAE, and F5's Vocos vocoder, have `entry_role: component`. Their pinned downloads
remain available internally, but they are excluded from ordinary model choices.
PersonaPlex is speech-to-speech; Dots editing variants belong to audio editing.
They remain discoverable under their actual tasks.

Voice modes, runtime selectors and compatibility labels are not independent
models. Service configuration retains them where a server needs them. Verified
VoxCPM and FishS2 aliases appear in `request_aliases` on the canonical row.
Provider identity matters: OpenAI's actual `tts-1` is distinct from a local
wrapper's same-named request label. Family-labelled entries do not establish the
exact weights deployed on a remote server.

`package_availability` is separate from controls and installation state. AuK
generator weights require manual shared-directory configuration with a
Qwen2.5-Omni conditioning component and VAE; automatic dependency binding is not
implemented. Irodori Anime's contract remains unverified and it is excluded from
speech choices. Catalogue membership does not certify inference or audio quality
on this host.

## Ways to influence generation

| Mechanism | Authored or configured as | Mapping and limit |
| --- | --- | --- |
| Voice identity | Narrator, character cast, category default, phrase voice | Each part resolves to a provider voice or reference; Pandrator generates and assembles the parts in sequence. |
| Designed voice | Saved provider resource or cast description | Gemini 3.8 voice creation saves a reusable resource. Request-description models condition each request; a reused description does not guarantee an identical voice. |
| Natural-language delivery | Project, character, block or phrase `<ins>` | Verified request field, quoted phrase hint, or inline tag. Description-only channels omit phrase delivery. |
| Emotion, pace, cadence, emphasis | Structured delivery fields or XML control elements | A documented enum, prose hint, or inline tag. Request-wide enums cannot target part of a request. |
| Vocal cue | `<event>` or selection `vocal_event` | Exact model vocabulary only. Pause duration and vocal timing remain model hints. |
| Before/after context | Enabled, bounded neighbouring text | Unspoken fields or prompt guidance on documented routes; separate from reference audio. |
| Reference conditioning | Published audio and its transcript where required | Voice cloning or identity conditioning; this does not establish support for acting instructions. |
| Model tuning | Typed request parameters | Sampling, output budget, guidance and other adapter settings; separate from prose direction. |
| Audio effects and gaps | Audio-processing and assembly settings | Processing after synthesis. A model pause cue is not inserted silence. |
| Sounds, music, attached audio | Separate task or media workflow | Upstream-only features do not become speech controls; arbitrary sound-file insertion is not implemented in speech XML. |

## Shared capability contract

`expressive_capabilities` comes from the resolver used by service discovery,
preview and synthesis. It records instruction kind, scope and destination;
required descriptions; saved-resource versus request-description design;
reference mode; emotion enums; exact event mappings; context transport; timing
limits; and sequential versus native multi-speaker support. Native multi-speaker
input is not implemented.

The short `capabilities` filter lists implemented controls. `upstream_features`
separately records streaming, dialogue, editing, conversion, sound and music
features. Azure's discrete voice styles use `azure_speech_style`; they do not
imply pSSML or general natural-language instructions.

The UI labels a control **sent**, **hint**, **not sent**, **unverified**, or
**off**. The compiler report describes the resolved request. Users may save
every valid annotation regardless of the current model. Unsupported authored
controls are reported and omitted; literal transcript words are preserved.
Pandrator XML is internal and is not forwarded as arbitrary provider SSML.

For Gemini 3.8, `instruction_scope: [request, span]` describes Pandrator's
compiler. `native_instruction_scope: [request]` and
`phrase_instruction_transport: quoted_hint` distinguish Google's actual
request-wide style field from Pandrator's approximate phrase targeting.
`instruction_transport` identifies `speech_metadata.style` for Google AI Studio
or `speechMetadata.style` for Vertex. These fields are shared by catalogue,
selection previews and synthesis; the UI displays their practical limits.

## Verified transport distinctions

| Model and route | Direction mapping | Important limit |
| --- | --- | --- |
| Gemini 3.8, Gemini API / Vertex | Structured speech style and unspoken context guidance | Phrase guidance and angle vocal cues are interpreted by the model. Voice design is a separate saved-resource operation. |
| Eleven v3/v4, native | Inline audio tags | No hard scope/reset guarantee. v4 has before/after context; v4 Turbo's realtime route has no documented hidden text context. |
| Qwen Base, audio.cpp | No delivery channel | Clone references change identity; natural-language directions remain saved and are omitted. |
| Qwen VoiceDesign, audio.cpp | Whole-request voice description | Phrase acting directions are omitted. CustomVoice 1.7B has a different delivery channel. |
| Maya1, audio.cpp | Required `options.instruct` | Voice and delivery description; no reference cloning on this route. Only verified vocal cues are compiled. |
| MOSS-TTS 1.5, audio.cpp | `options.instruct` | Whole-voice description hint; phrase delivery omitted. Reference conditioning is separate. |
| MOSS VoiceGenerator, audio.cpp | `options.instruct` | Request-description design; no reference input or verified vocal-event syntax. |
| Irodori 600M VoiceDesign / v4 Small, audio.cpp | `options.instruction` | Description and optional reference. 500M has no design channel; Anime remains unverified. |
| FireRed Instruct, audio.cpp | Description without reference | With a reference, the instruction slot is its transcript; additional delivery is omitted. |
| FishS2, native | Compiled inline cues and nested `fishs2` tuning | Legacy speed, latency, chunk, normalization and prosody-volume fields are omitted. |
| VoxCPM, native | No verified prose-direction mapping | Structured wrapper controls are distinct. Unverified prose directions and unused root speed are omitted. |

The audio.cpp compiler owns instruction aliases after merging tuning options,
avoiding conflicting aliases or stale top-level overrides. These checks establish
request construction, not audible adherence.

## UI and MCP authoring

In **Generation → Editing**, select words for directions, or place the caret
for a vocal cue. **Speaker & voice** requires project-wide **Multiple voices**.
Whole-segment instructions have a field beneath the text; phrase directions
have double underlines and colored notes below. **Save** compiles and validates
the draft; **Preview** optionally shows the request first. **XML** exposes
allowed actions in place with the same revision and lock checks. Casting and
delivery remain independent switches.

MCP clients read `pandrator_get_speech_selection_context` for the range. It returns
the XML action reference, flags, inherited directions, model capabilities and
voice preview. Use `pandrator_preview_speech_selection`, then
`pandrator_apply_speech_selection` with its preview revision and an idempotency key.
Omitted fields keep their values; explicit null clears only fields permitted by
the schema.

`vocal_event: {kind, position: before|after, duration_ms?}` can accompany a voice
or delivery edit. Duration is allowed only for a pause, from 50 to 5000 ms. XML
and field/cue edits are mutually exclusive. At a voice boundary, **after** belongs
to preceding words and **before** to following words; XML records
`affinity="previous|following"`. Delivery and vocalization switches are separate;
an edit can explicitly enable either. Annotations remain saved while off.

See [generation controls](generation-controls.md) for precedence, dictionary
editing, XML examples and protected annotations.
