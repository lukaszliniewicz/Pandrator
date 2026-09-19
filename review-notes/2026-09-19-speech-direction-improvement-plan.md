# Generation control plan: speech direction, dialogue, characters, and casting

Based on the [implementation review](2026-09-19-speech-direction-review.md) of `b8b53df9`. This is a proposed implementation sequence; application code has not been changed.

The goal is to make generation understandable and dependable across timed voiceovers and long-form narration: users can review dialogue structure, maintain stable character identities, cast voices at span level, and control delivery without corrupting accepted text or losing logical segment integrity. Speech direction remains independently useful; dialogue detection and multiple voices are optional.

This revision incorporates the user's expanded requirements: separate workflow behavior, male/female/androgynous voice defaults, named characters, a persistent character dictionary analogous to the glossary, and XML for speaker spans as well as instructions and emotions. Application code has not been changed.

## Shared concepts, different workflow behavior

Keep these concepts distinct:

1. **Text and structure:** accepted spoken words, paragraphs, dialogue turns, scene/chapter boundaries, and any source timing.
2. **Speaker identity:** a stable character or source-speaker ID, independent of the spelling of their name and the chosen TTS voice.
3. **Casting:** the mapping from a speaker, voice category, or explicit span override to a concrete voice supported by the selected backend/model.
4. **Delivery:** instructions, emotions, emphasis, pace, and events, independent of speaker identity.
5. **Rendering:** model-specific requests and their assembly into the user's logical segments and final audio.

| Concern | Audiobooks / long-form | Timed voiceovers |
|---|---|---|
| Structure | Paragraphs, dialogue groups, narrator asides, chapters, scenes | Source passages/cues, speakers, alignment groups, timing envelopes |
| Dialogue analysis | Detect quoted speech and turns; optionally resolve named characters across chapters | Preserve existing speaker labels/diarization; resolve names or missing spans where useful |
| Flow | Natural phrasing and intentional turn/paragraph/scene pauses | Fit delivery inside existing source timing and preserve synchronization |
| Context | Neighbouring paragraphs, relevant character-dictionary entries, scene/chapter boundaries | Nearby cues/passages, source speakers, timing evidence |
| Cast | One narrator, category defaults, or a named cast | Source-speaker mapping with optional finer voice spans inside a timed logical segment |
| Review | Text, speaker spans, cast, delivery and continuity | Those same controls plus timing feasibility and alignment evidence |

Dialogue detection must work in a single-narrator book. Multiple voices must work without emotional directions. A source paragraph break, a dialogue-turn break, and an internal TTS request boundary must not automatically receive the same silence policy.

## XML as the shared annotated speech format

Use XML for model-facing annotation exchange and the reviewed annotated document. Parse it into validated typed nodes/spans for the existing compiler and execution services. Existing JSON APIs can expose these parsed views; do not maintain separately editable XML and JSON sources of truth.

Illustrative syntax:

```xml
<segment id="s42"><dialogue><speaker ref="c7"><ins>Warm, with restrained amusement.</ins><em>happy</em>“Come in,”</speaker> said Scrooge.</dialogue></segment>
```

Here `c7` resolves through the character dictionary. Text outside a speaker element uses the narrator unless an enclosing scope specifies otherwise. The compact example intentionally preserves the spacing of the spoken text; the production serializer must not insert formatting whitespace into mixed text content.

Author-supplied shorthand such as `<speaker g="male" n="Scrooge">…</speaker>` is useful for import/manual markup. Resolve it to a dictionary entry and stable ID. Subsequent batches use compact references instead of repeating names, aliases, traits, and voice details throughout the document.

Rules for the first schema:

- `<segment id="…">` retains the input unit's identity and order. Annotation alone cannot split, merge, reorder, drop, or duplicate segments.
- `<dialogue>` identifies dialogue structure; it does not itself select a voice, add a paragraph pause, or rewrite wording. Groups may be associated across source units without flattening those units.
- `<speaker ref="…">` assigns identity to its enclosed speech. A nested `<span>` can apply a delivery control to a phrase within a speaker's turn. Explicit narrator spans are available for asides.
- `<ins>…</ins>` contains non-spoken free-form delivery instructions. `<em>happy</em>` contains non-spoken emotion metadata. These apply to their containing segment, speaker, or span; they are not indefinite switches affecting following siblings.
- Structured fields inherit from enclosing scopes, with the more specific value taking precedence. Instructions retain explicit global/local scope when compiled. No claim is made that a model perfectly obeys a phrase boundary.
- Reserve `<em>` for emotion in this custom dialect. Emphasis needs a distinct control; do not import HTML's meaning of `<em>` accidentally.
- Extract the clean transcript explicitly from speech-bearing content, excluding metadata nodes. Never flatten every XML text node with a generic `itertext()` call. Instructions, category labels, IDs, and emotion names must not enter synthesis transcripts, alignment, subtitles, length calculations, or word-based verification.
- Existing text optimization may create a changed, reviewable spoken version. A separate annotation-only operation must reconstruct that accepted version exactly. Preserve literal XML-like prose by escaping it; unknown markup is a validation error, not spoken text.
- XML parsing must reject DTDs/external entities, invalid nesting, and unresolved references, with bounded document size/depth. This is custom Pandrator markup, not SSML that can be sent unchanged to a TTS provider.

The XML representation avoids repeated quote anchors and dictionary definitions in the model's output. Measure actual token use and parse/text-preservation success on a small representative corpus with the chosen model before freezing syntax; XML is not guaranteed to use fewer tokens for every possible document.

## Character dictionary and cast

Maintain a **versioned character dictionary** through batches, document revisions, and resumptions. Initially scope it to the book/session, following the existing glossary's persistence pattern; reuse in another session should be explicit rather than a global merge of same-named characters.

Each entry needs a stable ID, display name, aliases, an optional voice category, short identity/voice notes, provenance, and accepted/manual-lock state. Name or alias edits retain the ID. A rename such as “Scrooge” to “Ebenezer Scrooge” must not create a new speaker. Keep uncertain attributions unresolved instead of inventing a confident identity.

Use **voice category/presentation** values `male`, `female`, `androgynous`, and `unspecified`. “Androgynous” describes the requested voice presentation, while “unspecified” means unknown or unassigned. If separate character sex/gender facts become useful, retain them as character facts rather than force them to equal the selected voice's category. Normalize this category for managed and provider voices; retain provider metadata and explicit user overrides.

Each analysis batch receives relevant dictionary entries and their revision. It returns annotations plus proposed additions/aliases. Accepted or locked identities are authoritative. Merge proposals deterministically with revision checks; parallel batches must not assign the same ID to different characters, silently merge namesakes, or recreate known characters under aliases. New provisional references become stable IDs only through dictionary acceptance/reconciliation.

The cast is separate from that dictionary. Bindings use validated voice references qualified by backend/model compatibility. Category defaults are casting preferences, not restrictions on artistic voice selection.

Voice resolution order:

1. Explicit span voice override.
2. Named/source-speaker cast binding.
3. Dialogue voice-category default, if set.
4. Narrator fallback, visibly reported for unresolved assignments.

Narration uses the narrator voice unless explicitly overridden. A character's name, category, or aliases do not themselves become provider voice IDs. Cast changes preserve character IDs and invalidate the audio for affected logical segments; dictionary notes/alias corrections that do not change the effective request should not force unrelated regeneration.

## Integral segments with internal voice parts

A logical segment remains one editable/reviewable unit with its existing source/timing association and take history. When its resolved voice changes inside the text, the renderer makes ordered internal synthesis parts, compiles their local delivery, and assembles them into one segment take. This is not a user-visible segment split.

Initially use voices compatible with one selected backend/model per run. Model-native multi-speaker requests can be supported later where the route provides a verified contract; they are not required for the portable implementation.

The part manifest records the clean text range, speaker ID, resolved voice/reference identity, effective directions, order, and produced duration. Inherited controls and phrase annotations must be projected and validated against each part's exact transcript. Independent audio calls must not accidentally apply sentence/paragraph silence multiple times.

For long-form narration, classify boundaries as continuation, dialogue turn, paragraph, scene, or chapter. Avoid treating dialogue's visual line/paragraph formatting as a demand for a long pause. Same-voice material can retain natural phrasing where the accepted plan permits it; narrator asides keep their own attribution.

For timed voiceovers, internal parts share their parent's timing envelope. Preserve actual source alignment evidence; do not invent precise speaker-transition timestamps by proportional text length. Fit and review the combined segment against its timing budget rather than assigning the full window independently to every part.

Retain segment-level take selection and regeneration in the first implementation. A cast change can regenerate affected whole segments while reusing unaffected ones. Sub-part caching and separate per-part take histories are deferred until their value is demonstrated.

## Current integration evidence

- `GenerationSegment` currently has one speaker and one provider voice (`pandrator/web/models.py:908`), and generation/batching operates per segment (`pandrator/web/workflow_handlers.py:8798`, `:8862`). Internal multi-voice rendering is new work, not a capability obtained simply by adding XML tags.
- Voice records have extensible metadata, but no common normalized voice category (`pandrator/web/models.py:1704`). The frontend already displays some provider gender metadata (`web/src/lib/voice-catalog.ts`); it is not yet a shared casting contract.
- Passive optimization preserves unit IDs/order and currently accepts SRT/JSON/TXT, not XML (`pandrator/web/speech_optimization_dispatch.py:46`, `:703`). Extend this contract without weakening its preservation invariants, and support the same format in configured-model processing.
- The actual glossary ledger exists in `pandrator/web/knowledge.py`; it has session/language scope, revisions, locks, and conflicts. `pandrator/web/dispatch.py:836-880` provides deterministic batch-wave snapshots and merges. Reuse these mechanisms, with character-specific IDs/alias handling; glossary term-string matching alone is insufficient for identity.

## 1. Make request preparation consistent and bounded

This remains the next executable phase. It fixes the backend correctness defects independently of dialogue/casting delivery, while preserving the concepts needed for the broader framework.

### Decisions

- **Saved generation settings govern a new run.** Preview resolves the same effective provider, model, direction, vocalization permission, and synthesis-context settings. Where a preview explicitly supplies draft overrides, it is identified as a draft preview. Run-local overrides retain their existing precedence.
- **Planning context remains separate.** A plan records the context and guidance used during analysis. Those historical values are provenance, not implicit overrides of current synthesis settings.
- **An adopted annotation preserves intent.** Model changes recompile that intent. They do not rewrite annotations or force another LLM analysis merely because the target model changed.
- **Existing runs retain their frozen inputs.** Later settings changes affect new runs; they must not alter an in-progress run's selected annotation or neighbour text.

### Changes

- Reuse a single synthesis-context preparation path in preview and generation. Remove preview's dependency on historical planning limits. Keep `_context_settings(plan)` for analysis.
- Extend the existing preview contract with optional before/after/character-limit overrides, so an explicitly labelled draft preview can represent every visible setting. Omitting overrides uses saved generation settings. Reflect these additive fields in HTTP, MCP, OpenAPI, and generated client contracts.
- Apply request budgets at the compiler boundary according to the resolved backend route. For Vertex, account for the UTF-8 bytes of the entire `contents` string: transcript, native tags, instructions, context, and envelope. The review verified the documented 8,000-byte limit; do not apply another API's limit merely because it uses the same model family.
- Preserve transcript and requested delivery instructions. Reduce optional context deterministically, preferring nearer blocks and removing farther material first; report the reduction. If the transcript plus required directions/envelope still cannot fit, return an actionable validation error before synthesis. Do not split the accepted speech block or silently remove its directions.
- Preserve speaker labels as complete units while trimming context text. Keep Unicode graphemes intact and retain section/language boundaries.
- Ensure the preview fingerprint describes the final budgeted provider input. Review compiler/capability versioning and audio-identity effects when changing compilation behavior; preserve existing takes and prevent reuse of audio whose effective request differs.
- Keep analysis permission for vocalizations and runtime permission distinct. Adoption must not silently enable vocalizations. Preview and its report must expose the effective runtime permission.

### Scope and ownership

Primary non-UI scope: `pandrator/logic/speech_performance.py`, `pandrator/web/speech_plan_workspace.py`, `pandrator/web/performance_plans.py`, preview schemas/routes, corresponding MCP/OpenAPI contracts, and focused tests. Touch provider transport only where necessary to enforce identical resolved-route behavior. No new provider, pipeline framework, automatic text rewriting, or database migration is planned for this phase.

The parent owns the settings-precedence and compilation decisions. An implementation specialist can receive the bounded backend packet once these contracts and the exact files are confirmed. The parent audits the result and owns any UI work.

### Acceptance

- For the same accepted text, annotation, resolved route, and saved settings, preview and a captured synthesis request agree on context, compiled input, instructions, and request options. Exercise the actual transport boundary with mocked network calls, not only two calls to the same compiler.
- Cover current settings differing from planning settings, explicit draft overrides, model switching, selected-block regeneration, and settings changes after a run is frozen.
- Cover ASCII and CJK context around the Vertex byte boundary, plus a case where the transcript/directions alone cannot fit. Context reduction is deterministic and visible; spoken text is unchanged.
- Truncation never leaves an unattributed fragment of another speaker's context or half a grapheme.
- Existing direction-disabled behavior, locks, adoption immutability, and source-staleness protections continue to work. Previously generated audio remains available.
- Run focused pytest and Ruff checks, relevant contract/type checks, and a scoped import/dead-code check if helpers move. Install the optional MCP test dependency in the development environment so transport coverage does not remain skipped.

## 2. Make editing and review trustworthy

After phase 1, fix the existing editor and expose the annotation clearly. This phase closes the remaining confirmed UI defects; it is not contingent on character detection or multi-voice synthesis. Keep the corrections focused so they carry forward into XML-backed editing rather than building a second full review interface.

- Use one parsed draft annotation for form and advanced views. While legacy JSON editing remains available, it must share this state; XML will become the annotated-document format in phase 3. Switching views preserves edits. Invalid advanced input remains editable with a validation error and cannot silently replace the draft.
- Track unsaved annotation edits separately from unsaved session settings. Progress refresh does not overwrite either. Preserve the selected block after Save. Offer Save / Discard / Stay only when an intentional navigation would lose changes.
- Bind a compiled preview to its exact inputs. Invalidate or mark it out of date when those inputs change, including model/settings changes outside the panel; discard late responses for superseded inputs.
- Make every active control visible: instruction, emotion, pace, cadence, emphasis, phrase ranges, and vocal events. Provide normal form controls for the common fields and a readable phrase/event summary. Adopted versions remain fully inspectable, including read-only JSON.
- Add an explicit **Clear block directions** action. Explain that general narration guidance can still apply; an empty local instruction does not mean a neutral voice.
- Make save scopes explicit. Adoption uses the saved draft and saved generation settings. If there are unsaved changes, resolve them through the save/discard flow rather than rely on a generic confirmation warning. Do not add routine repeated confirmations.
- Rename the feature **Speech direction**, its review step **Speech direction review**, and the optional LLM action **Analyse delivery**. Keep pSSML under advanced details.

Acceptance: browser regressions cover all five reproduced editor issues, read-only inspection of adopted controls, dirty-state navigation, stale async responses, keyboard use, and a narrow mobile viewport. Run Svelte/TypeScript and focused lint checks. The parent writes and inspects all UI code and tests.

## 3. Introduce XML, dialogue analysis, and the character dictionary

This phase establishes the shared representation and persists identity. It must work with an ordinary single narrator, before multi-voice rendering is available.

- Define the smallest versioned XML schema covering segments, dialogue, speaker references, narrator/phrase spans, instructions, and emotions. Implement validation, exact clean-text extraction, serialization, and adapters to the current performance compiler. Preserve existing sessions and accepted performance plans through an explicit compatibility adapter; avoid an unconditional rewrite of old artifacts.
- Add the persistent character ledger and voice-category metadata, using the existing revision/lock/conflict conventions. Freeze the relevant dictionary and cast revisions in subsequent generation snapshots.
- Extend speech optimization with independent options: preserve supplied markup; recognize dialogue structure; identify/reference speakers. These options do not require wording changes. Identity proposals remain separate from accepted manual assignments.
- Support both configured-model and passive/MCP processing, with the same validation and preservation rules. Pass neighbouring context and the relevant dictionary subset; do not repeat a whole book or the full character catalogue with every unit.
- Let the speech-direction analysis pass add or edit `<ins>`/`<em>` controls within the same accepted structure without rewriting spoken words or changing identities. Author-supplied instructions use the same validation. XML is parsed into provider-independent intent, then converted through the existing route-specific compiler.
- Add normal visual editing and read-only inspection for structure, identities, and directions. XML remains accessible as an advanced representation, not mandatory user work.

Acceptance outcomes: stable segment IDs and extracted text; exact distinction between markup and literal prose; scoped controls; stable character references across aliases, revisions, parallel batches, interruption, and resume; manual locks respected; ambiguous identity retained; directions-only and single-narrator modes still work. Include mixed narrator/dialogue sentences, multi-paragraph exchanges, unnamed speakers, namesakes, repeated phrases, CJK quotation styles, escaped XML characters, and malformed responses in a bounded fixture set. Measure tokenizer overhead and response-validation success on that set; stop once the format's essential invariants are demonstrated.

## 4. Add casting and complete the workflow-specific review

Scope this phase in detail after the XML/identity contract is validated. Its outcomes are defined now:

- Add category-default and named-speaker casting, with an explicit span override. Implement the internal multi-voice part manifest and composite segment take described above; cancellation or a failed part must not publish a partial segment as complete.
- Present structure/character review with speech optimization, and casting/delivery review before generation. Reuse the existing plan/job services. Compact summaries distinguish active adopted directions from newer drafts, unresolved speakers, text-stale directions, and changed model/voice compatibility.
- Use the separate long-form and timed-voiceover policies above. Validate paragraph/turn silences independently from provider request boundaries, and preserve parent timing envelopes in voiceover generation.
- Separate global voice/delivery settings, analysis setup, and block review in the interface. For VoiceDesign, keep the stable voice description visibly distinct from per-block delivery so a local emotion change does not appear to redefine the speaker. Load the selected plan's actual saved analysis configuration when showing its provenance.
- Replace the review dropdown with a compact navigable block list. Include full-plan filters for directed, unreviewed, unsupported, locked, dialogue, and unresolved-speaker blocks; do not filter only the current page. Show neighbouring context, rationale, readable annotations, character/cast assignments, and unambiguous version labels. Reuse pagination; avoid loading an entire book into the browser.
- Show analysis progress and a job link using the existing job system. Updates preserve local edits. Do not add a second scheduler or duplicate job state.
- Show model-specific results in ordinary language: encoded, approximate, unsupported, or disabled, with affected blocks and reasons. Preserve unsupported intent when switching models. Changing the model requires recompilation/compatibility review, not automatic reanalysis or deletion of annotations. Source-text changes still invalidate anchors and require a matching new plan.
- Before generation, expose material compatibility changes and hard errors. Standard approximations should be visible without a confirmation for every block. Deliberately dropped controls need a clear choice to adjust them or continue with the supported subset.
- Expand the existing capability definitions only where required by a real route: request budgets, documented syntax, backend/version constraints, and focused contract fixtures. Avoid a plugin framework or a universal emotion vocabulary. Free-form canonical direction remains useful across models.

Validation ends with a small, fixed comparison on representative deployed models: ordinary narration, dialogue-aware single-voice narration, and cast dialogue, with and without directions. Include narrator asides inside a sentence, rapid alternating turns, a long same-speaker continuation, a timed voiceover segment with multiple voice spans, short context-dependent replies, emotion/phrase controls, a vocal event, and CJK text. Assess transcript fidelity, context/instruction leakage, voice consistency, unwanted gaps, and voiceover timing. Choose routes and a capped synthesis budget at the start of the phase. Do not equate a valid request or an “encoded” status with verified acoustic compliance.

## Delivery boundaries

Deliver phases 1 and 2 as independent reliability improvements. Phase 3 supplies the XML and stable-identity foundation while remaining useful with one narrator. Phase 4 adds casting execution and workflow-specific review after a concrete UX pass. Each phase must preserve ordinary generation with optional features disabled and have focused acceptance evidence. Detailed worker packets are prepared only for the next executable phase; the later milestones are not speculative file-by-file implementation plans.

Dialogue detection and character identification are now in scope. Defer autonomous full-book character/scene biographies, unrelated pronunciation rewriting, exact emotion/timing guarantees, implicit user-visible segment splitting for voice changes, arbitrary cross-provider casts within one run, per-part take histories, and additional model families. None is required for the proposed first implementation.

Evidence used: the completed Terra/xhigh backend review and Luna/xhigh provider research, plus a Terra/high `deep-researcher` mapping of current non-UI speaker/voice/optimization paths for this expansion. The parent inspected UI and the glossary ledger, audited critical backend anchors, and owns architecture, representation, integration, and all UI work. No external-model route was used for this expansion.
