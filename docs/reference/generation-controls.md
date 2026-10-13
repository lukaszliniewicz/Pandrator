# Speech directions, dialogue, and character voices

The project overview exposes **One narrator** and **Multiple voices** in
**Voices**, with a shared speaker dictionary and cast. **Generation** combines
speech editing with recordings, takes and history in one workspace.
Project generation settings hold the delivery and vocalization switches.
Dialogue recognition lives in **Optimize text for speech → Timing & settings**.
It can annotate the document without rewriting any spoken words. Emotional
directions and multiple voices are independent options.

## Character identity and casting

A character has a stable ID, display name, aliases, notes, voice category
(`male`, `female`, `androgynous`, or `unspecified`), review status, and lock.
Recognition can propose new entries; it does not replace accepted or locked
entries. Names and aliases resolve to a unique ID. Ambiguous names must be
resolved explicitly. Dictionaries belong to a session rather than a global
list of same-named people.

Casting assigns provider voices or published managed references separately
from those identities. Voice descriptions are available for models
whose capabilities support voice design. Voice categories can also be edited
in the Voice Library. A category is metadata, not a provider voice ID.

A whole-block voice override takes precedence over phrase/cast choices; an
explicit alternate-take voice takes precedence over the block override. With
no whole-block override, voice selection uses the first applicable assignment:

1. An explicit XML span voice.
2. The named character's cast voice.
3. A source speaker's cast voice.
4. A dialogue category default.
5. The narrator cast voice.
6. The session voice.

An explicit `<narrator>` bypasses source-speaker and dialogue-category
assignments. Preview lists the resolved voices and fallbacks. All parts in a
run use the selected service and model; a binding qualified for a different
service/model fails validation rather than changing the run's provider.

## Recasting recorded voices

After changing the saved cast, use **Regenerate changed voices…** or the
regenerate action beside an individual speaker. The preview shows the affected
recordings, shared segments, and the project's current speech service and model.
Unchanged and missing recordings are excluded unless explicitly requested.
Explicit block or phrase voice overrides continue to take precedence.

A shared segment is generated as a whole, preserving the recorded assignments
of unselected speakers. If an earlier reference cannot be preserved, the preview
asks you to include that speaker or restore its reference. Existing audio remains
selected until its replacement succeeds; a newer manual take choice wins.

Each recast has **Before** and **After** audio versions in generation history and
Output. These preserve exact take choices for playback, assembly and export.
Failed replacements retain their earlier audio. **Undo this recast…** restores
the prior audio choices when its guard still matches, keeping the saved cast and
project settings. It does not delete either historical version.

MCP follows the same contract: use `pandrator_preview_recast`, then
`pandrator_recast_voices` with the preview's selection hash and an idempotency key.
Null selectors choose changed voices; an empty list chooses none. Preview and
apply undo through `pandrator_preview_undo_recast` and `pandrator_undo_recast`.
Historical segment reads, assembly and export accept `audio_version_side`
(`before` or `after`, default `after`).

## Review and edit in the Generation tab

Generation opens the effective TTS wording, including optimized text, and marks
optimized blocks. Double-click a segment, press F2 in its text, or use its pencil
to open the wording editor. Display options can hide the audio columns, show
direction details, switch to script/subtitle wording, and filter segments.
Hiding audio columns preserves the selected run and take choices. **Speech plans** contains the current
plan's review state, Mark reviewed, and details/version history. The help button
explains blocks, phrases, XML and keyboard controls without a permanent help bar.

Use a block number or its delivery button for whole-block actions. Select words
for phrase actions, or place the caret between words for a vocal cue. The compact
toolbar offers **Instruction**, **Emotion**, **Vocal cue** and further delivery
controls. Alt+Enter or Tab from the text focuses it; arrows choose an action and
Enter opens it. Preview text remains read-only; double-click, F2 or the
pencil button opens the wording editor for the selected text layer.
A **Whole-segment instruction** field beneath an
open segment editor edits only its root instruction. Display options can also
show these fields for all visible blocks. Use **Save instruction** or
Ctrl/Cmd+Enter. Phrase directions use colored double underlines and details on
hover, keyboard focus or touch. Optional direction notes appear below the text,
never inside the spoken sentence. Speaker assignments use single underlines;
vocal events remain at their positions. Historical runs show frozen settings.

**Speaker & voice** appears only when **Multiple voices** is enabled in the
project overview, for either a whole block or selected words. Settings refresh
after project changes without repeating catalogue downloads or discovery.
First identify the speaker: narrator, unnamed dialogue or a named person. The
autocomplete also creates new speakers. Voice presentation is optional; it does
not identify a person's sex. Names, aliases, notes and accepted/proposed status
can be corrected without replacing the stable ID used by AI annotations.

The speaker's shared cast voice is used by default. Open **Override voice** to
choose a voice only for the segment or selected words.
The cast/defaults section edits the project-wide identity, voice and delivery;
a selected-word voice override changes only those words. Saved and designed
voice identities are assigned through casting.
Provider voices remain selectable when their connection is unverified or
unavailable; the picker shows that state separately.
**Save** validates and compiles the draft before applying it. **Preview** is
optional when you want to review the whole block's resulting voice sequence first.
Saving a manual edit authorizes replacing its protected directions without a
separate unlock step. Stale previews cannot be applied. Automated edits still
require explicit authorization to replace protected annotations. The spoken
text and other phrases remain unchanged. Changes create a
new adopted manual direction plan, preserving previous plans and audio.

Each toolbar action opens a focused editor. A block's **XML** button opens the
XML editor in place. Fields have per-control model feedback:
**sent**, **hint**, **unsupported**, **unverified**, or **off**. A phrase hint
means scope is interpreted by the model; a separate resolved voice part can
receive its own request-wide instruction. Emotion choices show the model's
native values for a fixed enum, or suggestions for a model that accepts
descriptions. Custom values remain authorable. Unsupported choices stay saved
and are omitted from generation; feedback distinguishes this from delivery being
disabled in the project. Preview supplies the exact route's
final compilation report. “Sent” describes transport, not audible adherence.

For Gemini 3.8, whole-request style travels in `speech_metadata.style` through
Google AI Studio and `speechMetadata.style` through Vertex. A direction on
`stopped` compiles into a request-wide hint such as
`For occurrence 1 of 'stopped': emphasize this. Then resume the general delivery.`
This is an approximation: Google does not define a strict word-scoped style
boundary within the same text part. Whole-segment directions remain the general
style. Phrase hints contain only local differences and restore that general
style afterwards; inherited directions are not repeated as hints for surrounding
words. Explicit local resets use natural or neutral delivery hints. A voice
change can create a separate request, with uniform delivery applied directly to
that request. The focused editor
explains this distinction and can show the actual compiled style without
generating audio. See Google's [speech generation guide](https://ai.google.dev/gemini-api/docs/speech-generation)
and [Vertex migration guide](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/text-to-speech/migration-guide).

Casting and delivery are independent switches. Stored markings remain visible
when disabled. Qwen Base cloning can switch voices but does not support acting
instructions. The preview reports unsupported controls. A block voice override
still wins over a phrase voice until cleared in the block's voice settings.

Older structured JSON direction records remain editable in **Directions**.
The phrase popup refuses to convert nonempty legacy directions automatically,
so anchored directions or events outside a selection cannot be discarded.
When displayed text differs from optimized speech, select the speech layer;
unsaved text edits must be saved before adding annotations.

## Compact speech XML

Use a block's **XML** button to edit the markup directly in place.
**Allowed XML actions** lists the parser's vocabulary, attributes, enum values,
known speaker references, and limits. Preview and apply use the same protection
and revision checks as field edits. Changing mode, scope, or target requires
keeping or explicitly discarding an unsaved draft.

For the spoken text `He said, “Bah!” Then left.`, an annotation might be:

```xml
<segment id="SEGMENT_ID" boundary_after="paragraph">He said, <speaker ref="c-scrooge"><em>cross</em>“Bah!”</speaker> Then left.</segment>
```

`SEGMENT_ID` must be the exact ID supplied by the current stage. Passive speech
optimization uses numeric unit IDs; speech-direction plans use generation
segment IDs. Pandrator remaps validated IDs when importing annotations into a
new plan. `c-scrooge` must exist in the session dictionary or be proposed in the
same accepted speech-optimization response.

| Element | Purpose |
| --- | --- |
| `<segment id="…">` | One integral speech unit; optional `boundary_after`. |
| `<dialogue>…</dialogue>` | Dialogue with or without a known speaker. |
| `<speaker ref="…">…</speaker>` | A stable dictionary identity; implies dialogue. |
| `<speaker n="Scrooge" g="male">…</speaker>` | Resolve a known name/alias; canonical output uses `ref`. |
| `<speaker g="female">…</speaker>` | Category-only dialogue; no invented character identity. |
| `<narrator>…</narrator>` | Explicit narration, including an aside inside dialogue. |
| `<span voice="PROVIDER_VOICE">…</span>` | An explicit voice for a phrase. `speaker` and `narrator` also accept `voice`. |
| `<ins>Read gently.</ins>` | Nonspoken instruction for its containing scope. |
| `<em>happy</em>` | Nonspoken emotion for its containing scope. |
| `<em intensity="0.75">happy</em>` | Optional native whole-request emotion strength; actual bounds and support depend on the model/voice. |
| `<pace>slower</pace>` | `natural`, `slower`, or `brisk`. |
| `<cadence>questioning</cadence>` | `continuing`, `concluding`, `questioning`, or `contrast`. |
| `<emphasis>moderate</emphasis>` | `light`, `moderate`, or `strong`. |
| `<event kind="pause" duration_ms="200"/>` | Nonspoken event at that position. |
| `<sound cue="UUID" variant="UUID" placement="after"/>` | Derived independent project sound cue, stripped before TTS. See [sound placement](sounds-and-mixing.md). |

Other event kinds are `laugh`, `chuckle`, `sigh`, `inhale`, `exhale`, `cough`,
`gasp`, and `clear_throat`. Vocalizations require the corresponding generation
option and model support. Boundaries are `continuation`, `dialogue_turn`,
`paragraph`, `scene`, or `chapter`.

**Vocal cue** inserts an event at the caret, or before/after selected text.
At a cast boundary, an after cue belongs to the preceding voice;
a before cue belongs to the following voice. XML stores this as
`affinity="previous|following"`, defaulting to following except at block end.
A pause duration is a model hint, not inserted silence or an attached sound file.
See [model capabilities](model-capabilities.md) for the mapping distinctions.

Controls apply to the whole containing scope, wherever the metadata element
appears in that scope. Put a control inside `<span>` to target a phrase.
Instructions inherit; more specific structured delivery values override their
enclosing values. `ref` and `n` are mutually exclusive. A category-only speaker
needs a category other than `unspecified`; unknown identity can simply use
`<dialogue>`.

Range edits preserve direct root controls. The UI edits a phrase's local
contribution: clearing it restores the whole-segment direction. The MCP range
API retains its effective-value semantics: replacing or clearing a delivery
field can mask the root for the selected range. Rebuilt XML represents these
masks explicitly. To change only a whole-segment instruction, read the selection
context for the complete block, change its direct root `<ins>` in `speech_xml`,
and preview/apply the complete XML. Nested scopes, events and cue provenance
remain intact.

The extracted transcript must exactly match accepted speech text, including
spaces and quotation marks. Do not pretty-print mixed-content XML: indentation
would add spoken whitespace. Escape literal `&` and `<`. Unsupported elements,
attributes, declarations, custom entities, namespaces, malformed nesting, excessive
depth, and grapheme-splitting boundaries are rejected. XML is limited to
256 KiB, 2,048 elements, and 24 nesting levels per unit.

The provider receives a compiled request, never this XML verbatim. **Preview
model request** shows supported, approximated, and unsupported directions and
the exact compiled input. A successful compilation does not guarantee that a
speech model will obey every direction.

Inspect `phrase_instruction_transport` and `phrase_instruction_reliability` in
the capability profile. Fish and Eleven inline cues have best-effort scope; a
reset is another hint, not a closing tag. Whole-segment Fish cues are recommended.
Gemini 3.8 quotes selected phrases in whole-request style metadata and cannot
enforce phrase boundaries. Request-only models retain unsupported phrase controls
for another model and omit them from synthesis.

## Ways to influence generation

Pandrator separates authoring from the selected model's transport:

| Mechanism | Where it belongs | How support is determined |
| --- | --- | --- |
| Natural-language delivery | Book, character, block or phrase direction | A separate instruction field, prompt text or inline cue on the selected route. |
| Emotion, pace, cadence and emphasis | Structured delivery metadata | A native emotion enum where supported, otherwise a model-specific direction; semantic pace is distinct from numeric speed. |
| Vocalizations and model-interpreted pauses | Positioned XML events or anchored pSSML events | Exact route-specific tag mapping, plus the vocalization and delivery switches. Pause duration is a hint. |
| Narrator, dialogue, characters and voices | XML speaker scopes and project casting | Resolved provider voice or prepared managed reference; voice changes may split synthesis into parts. |
| Previous/following text | Generation context settings | Unspoken API fields or a prompt/metadata hint where implemented; text context does not establish audio continuity. |
| Voice description or reference audio/text | Voice design, cloning and casting | Checkpoint and serving-route support; a cloning transcript or identity description is not automatically a delivery instruction. |
| Numeric model settings | Model-specific generation settings | The selected adapter's accepted fields; for example sampling, speed or exaggeration. |
| Pronunciation and speech wording | Pronunciations and accepted TTS text | Applied before request compilation; directions never rewrite the accepted transcript. |
| Conversion, gain and assembly pauses | Audio conversion, processing and output settings | Applied outside the delivery compiler. These are distinct from model-generated vocal effects. |

Compact XML and legacy pSSML are Pandrator authoring formats, not arbitrary
provider SSML. There is no generic `<audio>` or `<prosody>` pass-
through. Valid unsupported metadata stays saved and visible while compilation
omits it. Literal transcript text is preserved, including text that resembles
provider tags. See [model identities and speech controls](model-capabilities.md)
for the differences between models and serving routes.

## Long-form and timed generation

Long-form generation keeps a dialogue exchange integral. `dialogue_turn` uses
the sentence pause; `continuation` avoids an added paragraph pause. Explicit
paragraph/scene/chapter boundaries use the configured paragraph pause.

A timed annotated cue retains its source timing envelope. Internal voice
changes do not fabricate subtitle timestamps or create additional visible
segments. Very large integral units can still exceed a provider's request
limit and need an explicit editorial change.

Casting renders internal voice parts into one logical take. **Voice-change pause
inside a segment** adds 100 ms by default when the resolved voice changes, for
example from dialogue to the narrator's "X said". Try 50 ms for a tighter join,
or zero to disable it. Natural silence already in the audio remains.

**Minimum pause at voice changes between segments** is a separate control,
disabled by default. The longer sentence, paragraph or clause pause wins;
explicit segment pauses take priority. Timed subtitle cues retain their timing.
Changing padding inside a segment requires a new take in a new generation run;
changing only assembly pauses can reuse existing takes. Saved takes and resumed
runs keep their original pacing.

A failed or canceled part cannot publish a partial segment take. Run snapshots
pin accepted annotations, identities, and casting. A new run uses current reviewed
settings. Audio reuse compares effective requests and voice references so
renaming a character or editing notes does not require new audio.

## Passive MCP workflow

For a direct edit, use `pandrator_preview_speech_selection` with the current
`revision_id`, `segment_id`, `expected_segment_revision`, and half-open `start`
and `end` offsets in Unicode codepoints. Select `0` through the codepoint length
for a whole segment, or a smaller range for a phrase. For a cursor, use equal
`start` and `end` and a `vocal_event`; omit speaker, character, voice, delivery
and XML fields entirely. Cursor context resolves the following span, or the
preceding span at block end. Pass a partial `delivery`
object (`instruction`, `emotion`, `pace`, `cadence`, `emphasis`) and optional
speaker/voice changes. Omitted fields stay unchanged; explicit `null` clears a
voice or delivery field. Inspect the preview, then send the same edit through
`pandrator_apply_speech_selection` with its `expected_preview_revision` and a
fresh idempotency key. Stale previews and locked directions are rejected; set
`unlock_locked` only when deliberately changing a lock. Enable casting or
performance explicitly if the edited directions should affect generation.

Use `pandrator_configure_tts` to automate model-specific defaults, including
style instructions, vocalization permission, and preceding/following context.
These settings require the selected model's advertised capabilities. Inspect
each compiled request part: a multivoice segment can have separate instructions
and provider options for each part.

The existing `performance` tool names remain stable for client compatibility;
the UI calls the stage **Speech direction review**.

1. Inspect `pandrator_get_generation_controls` and
   `pandrator_get_voice_catalog`. Update identities/casting with
   `pandrator_update_generation_controls`, supplying `expected_revision` and
   an idempotency key. Omitted sections remain unchanged; supplied character
   lists and cast objects replace their respective sections. Explicit
   `cast.narrator: null` clears the narrator assignment. Locked entries require
   explicit `unlock_ids` before editing. Use
   `pandrator_update_voice_metadata` with `expected_revision` and
   `changes.voice_category` to edit a managed voice's category.
2. Create `pandrator_create_speech_optimization_dispatch_run` with
   `annotation_mode: "dialogue"` or `"speakers"`. Set `annotation_only: true`
   to retain every spoken word. Claim, renew, or release work through the
   corresponding `pandrator_*_speech_optimization_dispatch_batch` tools.
3. Submit each actionable unit once and in order using
   `pandrator_submit_speech_optimization_dispatch_batch`. Supply
   `result: {"kind":"speech_optimization","items":[{"unit_id":1,"speech_xml":"…"}]}`.
   A duplicate `text` field is unnecessary. Put `character_proposals` beside
   `result`, using stable IDs, names, aliases, categories, and notes. Claims
   include the current dictionary and markup contract. Validation and proposal
   merging are atomic; expired leases must be reclaimed. Authored words,
   voices, directions, events, and boundaries cannot be silently discarded.
4. Review the resulting artifact and prepare its speech plan. If delivery
   analysis is wanted, create `pandrator_create_performance_plan` with
   `mode: "passive"`, `annotation_format: "xml"`, and the current
   `expected_plan_revision_id`. Claim and submit through
   `pandrator_claim_performance_batch` and
   `pandrator_submit_performance_batch`. Those submissions may add delivery
   intent while preserving the supplied transcript and speaker structure.
5. Inspect/filter with `pandrator_get_performance_plan`, manually edit or lock
   with `pandrator_edit_performance_plan`, and compile without audio with
   `pandrator_preview_performance_plan`. Preview accepts unsaved XML and
   direction/casting/context overrides. Adopt the saved version through
   `pandrator_adopt_performance_plan` when reviewed.

Creating passive runs, claiming/submitting markup, editing the dictionary,
previewing, and adopting do not invoke an LLM or synthesize audio. Generation
is a separate action. The configured-model analysis endpoints remain available
for users who explicitly choose them. Existing pSSML drafts remain readable
and editable alongside XML drafts.

## Editing generation text and reusing a run

In **Generation → Display options**, choose **Script / subtitle text** or
**Speech text**. Display edits keep the existing spoken wording, including
when speech previously followed the display text. Use **Use display text for
speech** (or **Reset to script** in the speech editor) to make both match again.
Speech-only edits never replace the display wording.

Selecting an older audio run also selects its saved generation settings. That
settings source survives text edits that create a new plan revision, and applies
to both segment regeneration and the Generate audio dialog. Provider, model,
voice, language and explicit TTS speed are retained; assembly slowdown remains a
separate setting.

Subtitle and voiceover exports use **Current display text** by default, independently
of the selected **Audio version**. To reproduce the wording saved with an older run,
choose **Subtitle text → Historical text from selected audio version** in Output.
The selected display revision is captured when the export is queued; later edits
do not change that queued export. Only its matching source
or translation track is updated. Verified cue mappings retain individual timing;
an edited merged block with an obsolete internal mapping uses its known overall
time window. Split pieces sharing a source cue appear once, and removed blocks
are omitted. Spoken overrides are never used as subtitle copy. Original subtitle
artifacts and historical plans stay unchanged.

A queued replacement is shown separately from the older recording's stale state.
If an export holds the session, Generation explains that generation is waiting
for it; queuing a replacement does not interrupt the export.


## Simple and Advanced interface

Every new project offers Simple (the default) or Advanced. The project header
lets you switch later and saves the preference. Existing projects retain
Advanced. Language workspaces use their shared project's preference; forks
inherit the source preference. AI assistants can use the same MCP workflows
in either mode.

Simple keeps compact progress/result cards and read-only Generation previews,
playback, search and output access. Advanced adds wording, cast/delivery and XML
editing, take selection, splitting and merging. A switch waits for wording saves
and refuses to hide an unfinished direction editor. Neither choice starts work
nor changes the workflow's Review each stage / Automatic execution choice.

Advanced previews are also read-only until double-click, F2 or the pencil opens
the wording editor. Phrase selection still offers speech actions; a caret and
cursor insertion actions belong to the explicit editor. Hover details read
the saved voice assignments and directions.
