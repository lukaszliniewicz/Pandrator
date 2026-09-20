# Voice discovery, design, and casting

Use `pandrator_get_voice_capabilities` first. It advertises this feature contract,
markup version, and per-model modes: prebuilt, cloning, design, and
reference_with_instructions. Known models are not necessarily installed or
available. Check `listed`, `available`, and the reason; refresh the TTS catalog
when readiness matters. Do not assume family names imply identical capabilities.

## Find and organise voices

`pandrator_get_voice_catalog` shares the application's search: query, language,
accent, voice_category, pitch, texture, use_case, collection_id, origin,
service_id/model, ready_only, reviewed_only, sort, limit and cursor. Restart the
query if its cursor is stale. Each result has a stable discriminated reference,
profile, compatibility, readiness, match reasons, and an optional safe_artifact_id.
Download that artifact through the ordinary artifact tool to listen.

Presentation is male/female/androgynous/unspecified. Pitch is low/mid/high;
perceived age is childlike/youthful/adult/older. Texture, delivery presets, and
suitability are independent multi-valued facets; retrieve `taxonomy` for their
vocabulary. Languages have a code, optional locale, accent and detail, and
optional evidence. A renderer's supported languages do not establish the voice's
native language or accent. Unknown is preferable to an invented trait.

`profile` has schema_version=1, pitch, perceived_age, textures, delivery_presets,
use_cases, languages, tags, and evidence. Evidence records have source
(user/provider/design_request/audition_review), status
(described/requested/reviewed), and optional artifact_id/note. Design requests
must remain requested. Reviewed traits require audition_review and an available
audio artifact. Update a managed profile with `pandrator_update_voice_metadata`;
provider profiles use `pandrator_update_catalog_voice_metadata`. These operations
replace a supplied profile; retain fields you intend to preserve.

Create a project group with `pandrator_create_voice_collection`; add/remove
members with `pandrator_update_voice_collection`, supplying its revision and an
idempotency key. Members use either `{kind:"managed",voice_id:"…"}` or
`{kind:"provider",service_id:"…",model:"…",voice:"…"}`. One voice may belong to
several collections. Membership never duplicates the voice or changes its sound.

## Create a reusable voice

1. Audition with `pandrator_audition_voice`: exact service_id/model, text,
   language, optional voice, generation_prompt, seed, and idempotency_key.
   For supported design models, omit voice and supply a voice description.
   Generate one candidate per call; agree on a bounded audition budget.
2. Follow `pandrator_get_work` to terminal state. Inspect the returned artifact,
   listen, and compare candidates on the same passage. Successful synthesis
   does not establish accent, intelligibility, or stable identity.
3. `pandrator_create_voice` stores a name, language, description, presentation
   and optional profile. Keep desired traits as design_request/requested.
4. `pandrator_promote_voice_design` takes the chosen preview artifact, its exact
   complete transcript, language, voice_id, current expected_voice_revision and
   an idempotency key. It checks preview provenance and content hash and queues
   normalization into a reusable reference. Poll to completion.
5. Alternatively, `pandrator_import_voice_reference` imports an existing managed
   audio artifact. Supply a transcript when available; transcript_reviewed=false
   preserves it as a draft. `pandrator_transcribe_voice_sample` can propose a
   transcript; inspect its work result and explicitly accept exact words with
   `pandrator_review_voice_transcript`. ASR alone does not mark text reviewed.
6. Inspect `pandrator_get_voice_samples`; publish/link the reference with
   `pandrator_publish_voice` for the selected service and current voice revision.
   Poll, then re-query the catalog to confirm compatibility/readiness.

Reuse the same idempotency key and identical request after an uncertain response.
A changed request needs a new key. Never create a replacement voice just because
a job is still queued. Metadata revisions and work IDs are distinct.

Qwen VoiceDesign creates a reference; Qwen Base clones it without directions on
this route. Breeze can design, clone, or clone with instructions when advertised.
Save the chosen audio reference before casting recurring characters. Reusing a
free-form description alone is not evidence of stable voice identity. Scottish
accent support must be auditioned; neither English support nor a prompt proves it.

## Cast and process the book passively

Read `generation-controls` for canonical XML, character identity, precedence,
preview and passive annotation. Keep character identities separate from global
voices. Assign stable managed voice IDs or exact provider voices, then freeze
and review the cast. A generation run uses one renderer; design may use another
model beforehand. The cast picker and MCP use the same catalog.

For A Christmas Carol, distinguish Scrooge from Marley even where the narration
says people call Scrooge by Marley's name. Cast a Scottish Scrooge only after
listening to a reference and separated lines. Process Stave I with
annotation_mode=speakers and annotation_only=true, preserve every word and
integral unit, review attribution and boundary choices, compile an early
Scrooge/nephew exchange, and audition it before the complete chapter. Narrator
asides must return to the narrator. Directions are optional; an instruction-free
renderer still uses dialogue structure and the cast.

Inspect the selected speech plan's revision/signature, record review, then use
`pandrator_generate_speech_plan`. Follow durable work, inspect takes, repair
specific segments, assemble, and export. Report structural and acoustic results
separately. Vocal events are capability-dependent; environmental sound effects
are not part of this speech renderer contract.
