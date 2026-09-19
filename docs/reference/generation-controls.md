# Speech directions, dialogue, and character voices

The **Speech direction** panel on the speech plan combines generation defaults,
the session's character dictionary and cast, and block-level direction review.
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
from those identities. Stable voice descriptions are available for models
whose capabilities support voice design. Voice categories can also be edited
in the Voice Library. A category is metadata, not a provider voice ID.

Voice selection uses the first applicable assignment:

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

## Compact speech XML

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
| `<pace>slower</pace>` | `natural`, `slower`, or `brisk`. |
| `<cadence>questioning</cadence>` | `continuing`, `concluding`, `questioning`, or `contrast`. |
| `<emphasis>moderate</emphasis>` | `light`, `moderate`, or `strong`. |
| `<event kind="pause" duration_ms="200"/>` | Nonspoken event at that position. |

Other event kinds are `laugh`, `chuckle`, `sigh`, `inhale`, `exhale`, `cough`,
`gasp`, and `clear_throat`. Vocalizations require the corresponding generation
option and model support. Boundaries are `continuation`, `dialogue_turn`,
`paragraph`, `scene`, or `chapter`.

Controls apply to the whole containing scope, wherever the metadata element
appears in that scope. Put a control inside `<span>` to target a phrase.
Instructions inherit; more specific structured delivery values override their
enclosing values. `ref` and `n` are mutually exclusive. A category-only speaker
needs a category other than `unspecified`; unknown identity can simply use
`<dialogue>`.

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

## Long-form and timed generation

Long-form generation keeps a dialogue exchange integral. `dialogue_turn` uses
the sentence pause; `continuation` avoids an added paragraph pause. Explicit
paragraph/scene/chapter boundaries use the configured paragraph pause.

A timed annotated cue retains its source timing envelope. Internal voice
changes do not fabricate subtitle timestamps or create additional visible
segments. Very large integral units can still exceed a provider's request
limit and need an explicit editorial change.

Casting renders internal voice parts and joins them without extra seam
silence into one logical take. A failed or canceled part cannot publish a
partial segment take. Run snapshots pin accepted annotations, identities, and
casting. Resuming a run keeps that snapshot; a new run uses current reviewed
settings. Audio reuse compares effective requests and voice references so
renaming a character or editing notes does not require new audio.

## Passive MCP workflow

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
