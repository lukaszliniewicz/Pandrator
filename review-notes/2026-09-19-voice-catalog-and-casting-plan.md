# Voice catalog, design, and autonomous casting

Proposal for the next feature, based on checkout `17af358f` and read-only MCP discovery on 2026-09-19. No application changes or audio generation were made during this planning pass.

The outcome is an end-to-end workflow: a user requests an unabridged, fully cast audiobook, specifies a Scottish Scrooge, and delegates the other casting decisions. An agent discovers capabilities and voices, designs missing voices, preserves their identity, annotates the source passively, reviews the compiled plan, generates and checks audio, and exports a chapter. Every step must be available through the real MCP connection.

## Product boundaries

Keep five concepts distinct and connect them with stable references:

1. **Voice profile:** a reusable voice's audible qualities and linguistic evidence.
2. **Voice realization:** a concrete provider speaker ID or reviewed reference sample usable by a particular model/mode. Extend the existing provider registrations and sample records rather than introduce a parallel asset system.
3. **Collection:** a user-defined group such as Voiceover, Christmas Carol, or Favourites. Membership is many-to-many and never copies or modifies a voice.
4. **Character and cast:** the existing session character dictionary identifies who speaks; the cast assigns a voice and optional role-specific delivery preferences. Scrooge's temperament belongs here, not in the reusable voice's identity.
5. **Performance:** per-run and per-span directions, emotions, pacing, and vocal events. These remain optional and capability-dependent.

The first release retains one synthesis service/model per generation run. A different model can design the reference beforehand if the final renderer can consume it. Arbitrary provider switching within a chapter is outside this phase.

## A small, orthogonal taxonomy

Use controlled facets for dependable filtering, a short free-text description for nuance, and optional user tags for organisation. Unknown is an explicit state. Facets are not mutually exclusive across dimensions.

| Facet | Initial vocabulary / representation | Meaning |
| --- | --- | --- |
| Voice presentation | Existing `male`, `female`, `androgynous`, `unspecified` | Preserve `voice_category` compatibility. Describes the requested/perceived voice, not a claim about a real person's biological sex. |
| Pitch | `low`, `mid`, `high`, unknown | Perceived habitual speaking register; independent of presentation. Optional acoustic measurements belong to a particular sample and must not redefine this label automatically. |
| Perceived age | `childlike`, `youthful`, `adult`, `older`, unknown | Optional casting description, not an inferred birth age. |
| Timbre / texture | Small multi-value vocabulary: `warm`, `bright`, `dark`, `airy`, `breathy`, `raspy`, `gravelly`, `resonant`, `clear`, `nasal` | Store a few useful descriptors; allow a free-text qualifier without creating an endless controlled list. |
| Languages and accents | Multiple language entries; optional locale plus accent label/detail, e.g. English / Scottish / Edinburgh | Separate languages a model can synthesize from languages and accents actually demonstrated by this voice. Broad Scottish English is valid; do not invent a more specific regional accent or conflate it with Scots or Gaelic. |
| Delivery presets | `neutral`, `conversational`, `formal`, `storytelling`, `dramatic`; optional pace/energy preferences | Reusable defaults or audition styles. They are not guarantees that every compatible model can reproduce the style. Per-span emotion remains in speech directions. |
| Suitability | `audiobook_narration`, `character_dialogue`, `voiceover`, `documentary`, `news`, `advertising`, `instructional` | Non-exclusive recommendations. “Professional audiobook” means suitability plus good evidence, not a distinct pitch or gender. |
| Origin and readiness | Built-in, designed, cloned/imported; reference/transcript review state; renderer compatibility; ready or unavailable with a reason | Operational facts shown separately from sound descriptors. Include provenance and the applicable model/source licence metadata. |

“Neutral” must be qualified: neutral delivery, mid pitch, and an unspecified accent mean different things. There is no globally accent-free voice.

Metadata needs a small evidence record: source (`user`, `provider`, `design_request`, `audition_review`), review state, and sample/artifact reference where available. A request for a Scottish voice records a **requested** trait; it does not become an audition-confirmed trait simply because generation succeeded. User corrections take precedence over automated proposals. Avoid pseudo-precise confidence percentages in the initial implementation.

Keep the legacy primary `language` field for compatibility, while the profile supports multiple language/accent entries. Existing records migrate with unknown new facets; do not infer them from names such as “German1.” Preserve existing descriptions, registrations, samples, and IDs.

## Search and collections

Expose one normalized catalog query shared by the UI and MCP. Include managed voices and provider voices without requiring users to clone or import every built-in entry. Use stable, discriminated references: managed voice ID, or provider service/model/voice ID. Collections can contain either kind.

The query supports text, language, accent, presentation, pitch, texture, suitability, collection, origin, and compatibility with the selected renderer. Return bounded pages, a cursor, facet counts, deterministic sorting, and concise match explanations. Default sorting is relevance when there is a query, otherwise name; also offer recently added/used. Collection membership and user metadata should survive catalog refreshes.

Distinguish hard constraints from preferences. For “Scottish Scrooge,” English synthesis, a Scottish accent, and compatibility are requirements; older, low, or gravelly can be preferences chosen by the casting agent. Unknown accent evidence is not an exact match. If no voice qualifies, return the missing requirements and a compatible design/clone route rather than silently substituting a vaguely British voice.

Start with normalized fields, ordinary text search, and deterministic ranking. The host model can translate natural-language requests into these filters. A vector database and automated whole-library audio tagging are unnecessary for the first release.

Collections organise assets; a session cast assigns roles. A book may link to a collection, and a voice can serve several characters or books with different direction presets. Removing a collection membership must not remove a cast binding or delete audio.

## Voice design as a complete lifecycle

Reuse the existing preview, promotion, normalization, transcription/review, and provider-publication jobs. Expose their missing operations through typed MCP tools and the same backend services used by the UI:

- Create/update a voice profile and manage collection membership.
- Discover a design or cloning route, including reference requirements and compatible final renderers.
- Request a bounded audition set with an exact spoken text and design brief.
- Retrieve job state, audio artifacts, transcript, requested traits, seeds/settings, and model provenance.
- Record audition review and select/promote a candidate to a managed voice sample.
- Import an approved local/artifact reference, transcribe it, review/correct its exact transcript, and register it with the chosen renderer.
- Audition a saved voice on new text, then assign it through the existing cast tools.

A useful default for delegated casting is at most three candidates for a missing principal voice and one refinement round, within a declared overall run budget. Reuse existing voices for other roles where suitable. The user can delegate selection (“take care of casting the rest”); this must not introduce an obligatory approval click for every character.

The reusable identity is the selected reference/provider speaker plus provenance, not merely a description or random seed. Pin the accepted sample and registration revision in the generation snapshot; a later profile edit or alternative sample must not silently change a resumed chapter. Keep neutral reference audio separate from dramatic audition clips so a shout or exaggerated emotion does not become the accidental default identity.

Mutations use the project's revision checks and idempotency conventions. Long-running auditions, normalization, publication, and generation return durable work IDs, support cancellation, and expose resumable next actions. Retrying after a lost response must not create duplicate voices or regenerate an already accepted audition.

## Capability discovery and passive operation

The agent needs a compact discovery response describing the installed app/MCP build and schema versions, current target, known models, installed models, readiness, and available operations. Do not conflate “supported by the catalog,” “installed,” “service running,” and “usable now.” Filter nested model/capability maps as well as the top-level list so a narrow query does not return every unrelated model.

Model capabilities must be specific to the backend, exact model variant, version, and mode. They should describe combinations, not independent booleans that suggest unsupported combinations:

| Mode | Questions discovery must answer |
| --- | --- |
| Built-in speaker | Which IDs and languages? Can this identity receive directions? |
| Reference cloning | Which audio formats and transcript requirements? Which languages? Can directions and a reference be used together? |
| Voice design | Which description field and languages? How is the result adopted for stable reuse? |
| Speech controls | Request/span scope; supported emotions/vocal events; approximations and unsupported controls |
| Resources | Download/install/start requirements, current availability, relevant licence, and known limits; measured speed estimates only where evidence exists |

The official Qwen workflow is VoiceDesign → generated reference → Base cloning. Qwen Base has no documented instruction-control path equivalent to 1.7B CustomVoice, which uses its packaged speaker identities. This tradeoff must be explicit when choosing the final renderer. [Qwen documentation](https://github.com/QwenLM/Qwen3-TTS#voice-design-then-clone)

Breeze TTS 2 documents separate design, clone, and reference-plus-direction modes in English and Chinese. Scottish-accent fidelity still requires an audition. The local adapter must verify that it exposes the same combination before discovery advertises it as usable. Its model card also distinguishes its research/non-commercial self-hosted licence from its code licence. [Breeze model card](https://huggingface.co/BreezeBlue/Breeze-TTS-2), [audio.cpp 0.8.1 adapter documentation](https://github.com/0xShug0/audio.cpp/blob/v0.8.1/docs/models/breeze_tts.md)

Keep the XML contract and character dictionary in the current passive claim/submit workflow. Compact discovery should point to the versioned markup contract and worked examples; every claimed batch supplies the exact IDs, relevant dictionary entries, and preservation rules. No new markup language is needed for catalog metadata.

Offer three independent processing choices: dialogue identification, named-speaker attribution/casting, and optional speech direction. A renderer with no instructions can still run the complete named-speaker pass and render the cast. Unsupported required controls must be reported before generation; optional controls can be omitted only under an explicit policy captured in the reviewed plan.

Passive means the host agent performs the analysis and submits reviewed results; Pandrator does not start an additional LLM analysis behind its back. Synthesis and optional transcription remain explicit durable media jobs. Guides, catalog descriptions, and recommended next steps must connect the whole procedure, including review, recovery, targeted regeneration, and export.

Vocal events such as sighs and laughter belong to speech controls. Chains, doors, music, ambience, and processing such as reverb need a separate sound-design/mixing layer. Do not send environmental effects as spoken prose or promise that a TTS event tag can generate them. Full environmental sound design is deferred; capability discovery must distinguish it from supported vocal events.

## Library and casting UI

The current reference view is a name/language list beside a detailed sample editor. The pre-built view starts with service/model selection. The redesign should make discovery the default task:

- One library search across origins, a collection selector, visible active-filter chips, result count, and sort.
- Desktop filter panel and results with name, a short audible description, language/accent, a few useful tags, compatibility, and immediate playback.
- A voice detail view for samples, full provenance, metadata editing, design variants, transcripts, and provider links. Recording and publishing should not occupy the default browsing surface.
- A comparison tray for a small shortlist, reading the same passage. Generating previews is explicit, with candidate count and progress; searching must never synthesize audio automatically.
- A session cast view with character, requested traits, assigned voice, audition, and any unresolved mismatch. It uses the same catalog picker, filtered to the session renderer.
- Design can start from an unmet casting requirement and return the accepted voice to that character and the book's collection without manual ID copying.

On mobile, use a single-column result list, a filter drawer, and full-width voice details/designer. Keep the integrated navigation header compact; avoid a persistent filter sidebar plus a second sticky toolbar. Preserve the query, filters, scroll position, and audition state when returning from details. Keyboard operation, labelled playback controls, focus restoration, long names, empty results, and unavailable providers are acceptance cases.

Implementation visual checks cover approximately 390 px, 768 px, and desktop widths. This planning pass inspected UI source; the managed live page required sign-in, so no authenticated rendered-library review was completed here.

## Delivery sequence

### 1. Catalog and discovery foundation — next executable phase

Add a validated, versioned profile under the existing voice metadata, first-class collections/membership, and a normalized read/query service for managed and provider entries. Reuse existing provider registrations and artifact storage. Keep profile normalization/search in the backend so UI and MCP do not independently infer traits from voice IDs.

Extend HTTP and MCP inventory/update contracts with the facets, evidence states, compatibility summaries, cursor/sort/filter semantics, and collection operations. Keep current tool names and simple language-only calls working. Add the compact discovery/build information and model-mode capability combinations needed by the lifecycle.

Acceptance: existing voices/registrations round-trip unchanged; a voice can belong to multiple collections; accented-language queries do not confuse model language support with demonstrated accent; unknown metadata remains unknown; stale revisions are rejected; UI/API/MCP queries agree; catalog refresh preserves user labels and membership; response size remains bounded.

### 2. Complete design/casting workflows and library UI

Expose the existing voice lifecycle through MCP, connect it to passive casting and recommended next steps, and implement the library, comparison, design, and cast interactions above. Reuse backend jobs rather than create a second MCP-only synthesis path. Review and exercise renderer-mode combinations before advertising them.

Acceptance includes idempotent retries, cancellation/recovery, immutable selected references, a working instruction-free rendering path, supported direction with cloning where available, and responsive visual checks performed by the parent agent.

### 3. Run the real Christmas Carol acceptance scenario

Use the protocol below after component and integration checks pass. Refresh/reconnect the managed app and MCP as needed so the real tool inventory exposes the tested features. A source checkout passing tests is insufficient.

## Christmas Carol acceptance protocol

Use [Project Gutenberg ebook 46](https://www.gutenberg.org/ebooks/46), Stave I, “Marley's Ghost.” Record the exact source URL, retrieval date, file hash, chapter boundaries, and accepted spoken-text hash. Review cleanup of front matter, illustrations, and hard-wrapped lines before speaker annotation. Preserve the chapter's words; a fully cast reading must not silently become an abridged adaptation.

Run through the actual MCP connection starting from the user's natural-language request. Shell or database shortcuts must not perform missing product operations. Store the tool trace and a compact run manifest with source/artifact revisions, cast, voice samples, backend/model versions, settings, durations, and review outcomes.

1. **Discover and prepare.** Verify the tool/build contract, source import, available renderers, voice search, XML schema, passive dispatch, artifact review, generation, and export. Identify an executable path within the declared local resource and audition budget. Installing a model is distinguishable from selecting an already available one.
2. **Analyse the chapter passively.** Create stable identities for actual speakers, including narrator, Scrooge, nephew, clerk, Marley, and any other attributed voices. Use source-supported names/aliases; do not require invented canonical names for people not named in the selected text. Keep narrator asides and rhetorical/hypothetical quotations separate from spoken dialogue.
3. **Cast and design.** Search before creating voices. Design at least Scrooge if no verified suitable Scottish voice exists; exercise the design/promote/register path even when the rest of the cast uses existing voices. Compare at most three candidates and one refinement round for this required role. Review the selected voice on a neutral sample and two separated chapter passages before generating the whole chapter.
4. **Compile a small exchange first.** Use the early Scrooge/nephew exchange to check alternating voices, embedded narrator asides, exact transcript preservation, and dialogue pauses. Exercise both a directions-capable route and an instruction-free clone route on this bounded excerpt where usable models are available. A unit-test mock does not count as the live route check.
5. **Render the full chapter.** Use one selected renderer and frozen cast. Review the plan before launching generation. Complete the chapter once the excerpt passes; do not generate the whole chapter repeatedly to explore model choices.
6. **Review, repair, export.** Inspect artifacts and listen to the full chapter once. Target any failed segments for regeneration, then export the assembled chapter and cast sheet. If the host cannot actually inspect audio, obtain a listening review and leave acoustic acceptance pending until it exists; text transcription alone is insufficient evidence for accent or voice identity.

Pass criteria:

- Every accepted speech unit appears once and in order; metadata and XML are never spoken. Automated exact-text comparison and parser checks have zero mismatches.
- Every speaking span has an intentional resolved assignment or an explicitly reviewed fallback; no accidental unknown-character fallback passes unnoticed. Review all chapter attributions before synthesis, with zero known wrong-speaker assignments remaining.
- Scrooge and Marley remain distinct despite the text saying that some people call Scrooge by Marley's name. Repeated descriptions/aliases resolve consistently to their intended characters.
- Scrooge's selected and rendered voice is recognisably Scottish and intelligible; its identity persists across the separated audition passages and the full chapter. Record the listening reviewer and outcome rather than claiming the design prompt proves the accent.
- Major characters are audibly distinguishable. Narrator asides do not inherit the adjacent character's voice. Dialogue exchanges have no unintended paragraph-length pauses or audible internal-join defects.
- There are no unreviewed omissions, repetitions, truncation, clipping, or spoken control tags. Transcription/alignment can flag candidates for review but is not the sole acoustic judge.
- The instruction-free route preserves the same text, attribution, and cast without sending unsupported directions; the expressive route reports the directions/events it actually compiles. Real acoustic success is reported separately from compiler success.
- A bounded cancel/resume test and a deliberately interrupted client response do not create duplicate voices, duplicate takes, or published partial segments. Changing Scrooge's cast reference invalidates affected audio while unrelated effective requests remain reusable.
- Library filtering, collection membership, audition, and cast assignment work on desktop and mobile. The exported chapter can be played and its provenance/cast recovered through MCP.

Record synthesis wall time and audio duration for later estimates, but set no invented universal speed or quality score. Stop auditioning at the declared budget; report a failed accent or unusable renderer honestly rather than relaxing the user's requirement silently. Environmental soundscape generation is not a gate for this first fully cast audiobook test.

## Evidence and remaining limits

- Managed storage and provider registrations already exist: `pandrator/web/models.py`, `pandrator/web/voice_library.py`.
- Voice preview, promotion, normalization, and publication already exist behind HTTP/jobs: `pandrator/web/api_routes.py`, `pandrator/web/workflow_handlers.py`.
- Current MCP voice inventory projects only ID/name/language/RVC/revision and lacks searchable casting traits: `pandrator_mcp/tools/inventory.py`.
- Source now contains generation-control and voice-metadata tools, but the loaded MCP connection did not advertise them. Live discovery reported app 0.9.4 and 14 managed voices; it did not establish an executable Breeze/design route. Refresh and verify the actual installed build/tool contract before the acceptance run. An older version directory appeared in the FFmpeg path; that alone is not proof of the app's active commit.
- Current UI discovery and design source was inspected in `web/src/lib/VoiceManager.svelte`, `PrebuiltVoiceLibrary.svelte`, `VoiceDesignDialog.svelte`, and `voice-catalog.ts`.
- No live accent, model speed, cross-backend identity, or acoustic-quality claims were validated in this planning pass.
- Evidence specialists: GPT-5.6 Terra/high mapped non-UI backend/MCP contracts; GPT-5.6 Luna/xhigh verified primary Qwen/Breeze/audio.cpp documentation. Product decisions, taxonomy, UI inspection, and integration plan remained with the parent agent. No external OpenCode route was used.
