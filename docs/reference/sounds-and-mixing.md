# Sounds, music and project mixing

Sound effects and music are an independent audio layer: changing
the speech model or voice retains the saved sound and its project cue. Speech
generation does not receive environmental sound markers.

## Playground and library

Open **Sounds & Music** from the application menu. In **Playground**, choose
Sound effect or Music, select a model, describe the clip and generate a candidate.
Models that accept lyrics reveal a lyrics field; their additional controls are
under Advanced settings. The player appears as soon as a candidate is ready;
audio is fetched when played. **Save**, **Discard** and **Regenerate** act on the
candidate. Import a recording through the same page. **Library** searches saved clips, shows
variants and allows renaming, archiving, choosing the preferred variant and
adding another variant. Existing project cues keep their exact variant when
the preferred variant changes.

The generators use audio.cpp's `gen` operation independently of the speech
catalogue. Stable Audio 3 Small SFX Q8 remains the default: four seconds, four CPU
threads or the available CPU count if lower, eight inference steps and guidance
1. Other models have their own duration limits, controls and seed range. See the
[model and translation map](audio-generation-models.md). An omitted seed is
resolved once at admission. Retrying the same idempotent request retains its
candidate, job and seed. **Regenerate** chooses a new seed unless a seed was
explicitly entered.

Native work uses the existing audio.cpp resource locks. CPU sound generation
does not change the project TTS settings or unload the resident speech model.
Generated variants record the kind, prompt, lyrics, resolved parameters, model revision/hash
and runtime version/hash. Originals remain immutable; playback trim, gain and
fades belong to the cue and produce derived mixes.

Saved sounds and unsaved candidates are global, with no project ownership.
Discard applies to an unsaved candidate; saved sounds can be archived.
References from project histories protect pinned variants from deletion.

## Adding a sound to a project

In Advanced Generation, use **Sound effect** at the caret or the sound controls
to choose a saved variant. **Vocal cue** remains a distinct speech-model action
for sighs, chuckles and similar utterances. Simple mode previews existing
effects; it does not remove agent/MCP authoring capabilities.

| Anchor | Meaning |
| --- | --- |
| `before_segment` / `after_segment` | A boundary in the selected speech plan. |
| `after_word` | The quoted occurrence in the actual spoken transcript, with a tight boundary search. |
| `next_pause` | The quoted occurrence, permitting a later pause within a bounded window. |
| `source_time` | A fixed time in a timed project. |

Word anchors retain Unicode code-point ranges and quoted occurrence identity.
Caret selection inside a word anchors the complete word. The spoken transcript
uses TTS-optimized text when present, so display text cannot silently supply a
different timing reference. Repeated or changed words can require placement
review.

Playback supports insertion or overlay, offset, gain, trim and short fades.
A new music cue defaults to overlay; existing cue modes remain unchanged.
Reduce its gain and trim it to the available project audio for background music.
An audiobook sound effect defaults to insertion. It shifts later narration, chapter offsets
and generated captions together. Overlay does not lengthen the narration.
Timed voiceovers use overlays, preserving source duration and subtitle windows.
Source soundtrack and speech are mixed first, then effects. Subtitle-only
projects retain sound metadata without synthesizing speech or adding captions.

Cue changes create a version of the project sound plan. They invalidate the mix,
preserving speech takes. Split/merge/resegment, selected-plan replacement and
forking preserve variant pins; ambiguous internal boundaries become **Needs
placement**. An included unresolved cue blocks mixing. Explicitly exclude it or
change its placement rather than expecting it to disappear from the output.

**Render mix** builds a derived output from a base assembly. Generation keeps
the dry assembly ID and returns a separate mixed artifact ID. The assembly
player/download uses a current mix when one exists; sequential **Play speech**
auditions the speech takes. Output's `include_sounds` option defaults to true.
With it disabled, or without included effects, the established output path is
unchanged. Missing/stale mixes are not presented as current outputs.

## Word resolution and acoustic safety

**Resolve placement** is a durable job, with at most 20 word cues per batch.
Default selection processes pending cues first, then ready cues whose take or
transcript pins became stale. Current ready pins are skipped. Re-read the plan
after completion and continue if `remaining_count` is nonzero.

The worker pins the completed selected take, artifact ID, audio content hash,
exact transcript hash, language and alignment/runtime settings. It reuses an
alignment per matching take and verifies the files before a short publication
transaction. A changed take, plan, lease or cancellation prevents stale results
from publishing.

For insertion, forced alignment identifies a neighbourhood. The waveform check
requires a sustained 80 ms quiet interval, a conservative -55 dBFS energy limit,
30 ms lexical protection and VAD evidence. These defaults favor preserving
speech. If no safe interval exists, the cue remains pending with reason
`no_safe_quiet_gap`. Overlay can use the aligned word boundary because it does
not cut the speech waveform. Choosing overlay is explicit.

If insertion cannot find a safe gap, review the placement or choose overlay.
These checks reduce the risk of cutting spoken audio, but you should still listen
to the result before exporting.

## Internal XML

Project sound plans are canonical. The XML editor derives representable sound
markers, for example:

```xml
<segment id="SEGMENT_ID">The bell rang.<sound cue="CUE_UUID" variant="VARIANT_UUID" placement="after"/></segment>
```

Only `cue`, `variant` and `placement` attributes are accepted. Cue/variant IDs
must be UUIDs and the variant must resolve to ready managed sound audio.
Placements are `before`, `after` and `next_pause`. A `before` marker belongs at
the segment beginning; `after` at the end denotes its boundary, while an internal
marker identifies preceding spoken words. `next_pause` denotes a word anchor.
Markers contain no text, paths, URLs or generation requests. Duplicate cue IDs,
unknown attributes and unsafe XML are rejected.

Preview reports added, changed and removed sound cues alongside speech changes.
Apply uses both speech-context and sound-plan revision guards in one transaction.
Sound-only edits do not change the speech signature. Sound markers are stripped
before speech compilation; `<event>` retains its model-dependent vocal meaning.
Timed source anchors, playback parameters and unrepresentable cues remain in the
sound plan and are edited through the chooser/MCP. The editor reports omitted
markers rather than claiming a complete XML representation. Per-segment XML is
bounded to 32 sound markers; a projection reads at most 40 to detect truncation.

For speech instructions and native emotion intensity, see
[generation controls](generation-controls.md). Index intensity lowers to
`emotion_alpha` (0–1); supported Azure voice styles use `style_degree` (0.01–2).
Both controls apply to the whole request, with segment/character inheritance.
Phrase-specific intensity is retained with feedback and omitted, rather than
inventing a phrase transport or a universal intensity scale.

## MCP and HTTP contracts

Discover the current server schemas; do not reuse a schema from a prior build.

| MCP tool | Purpose |
| --- | --- |
| `pandrator_sound_catalogue` | Separate effects/music models, capabilities and runtime availability. |
| `pandrator_generate_sound` | Queue a candidate with model, kind, lyrics and validated controls; returns work and resolved settings. |
| `pandrator_sound_library` | `list`, `get`, `candidates`, `candidate`, `save`, `update`, `discard`. |
| `pandrator_import_audio_reference` | Typed approved-root import, targeting `sound` or `voice_reference`. |
| `pandrator_project_sounds` | `get`, `status`, `add`, `update`, `remove`, `restore`, `resolve`, `render`. |

Writes require idempotency keys; item/cue edits additionally use their current
revision. Use `pandrator_get_work` for durable jobs. Lists are bounded and omit
provenance by default; `include_detail=true` fetches one item's/candidate's safe
provenance. Project cues expose compact placement status/reason without alignment
transcripts or internal paths. Import files must be contained regular files in
an approved root; the MCP limit is 16 MiB and HTTP streamed imports limit 50 MiB.

HTTP uses `/api/v1/sounds` and
`/api/v1/sessions/{session_id}/sound-plan`, with separate catalogue, candidate,
import, history, render and resolve operations. Read routes require `app.read`,
editing routes `app.write`, and generation/resolution/rendering `app.run`.
Check the generated OpenAPI document for exact bodies.

## Module ownership and performance

| Surface | Responsibility |
| --- | --- |
| `logic/audio_cpp_sfx.py` | Compatible generation facade, runtime identity and output/process validation. |
| `logic/audio_cpp_generation_models.py` | Allowlisted generation packages, controls, defaults and request normalization. |
| `logic/audio_cpp_generation_assets.py`, `audio_cpp_assets.py` | Immutable asset manifests and shared verified, cancellable downloads. |
| `logic/audio_cpp_generation_payload.py` | Model-specific CLI translation; clients cannot supply native paths or options. |
| `web/sound_service.py`, `sound_library_*`, `sound_generation_*`, `sound_imports.py` | Global library, admission, immutable publication and typed import. |
| `web/sound_project_*`, `sound_*revision*` | Versioned project cues, topology/history and read projections. |
| `logic/sound_boundary_refinement.py`, `web/sound_resolution_*` | Exact-transcript alignment, acoustic refinement, cached evidence and fenced resolution. |
| `web/sound_mix_*`, `sound_book_*`, `sound_workflow_outputs.py` | Frame-based rendering, manifest identity and book/timed export integration. |
| `logic/speech_sound_markup.py`, `web/speech_selection_sounds.py` | Derived XML and transactional compound edits. |
| `web/sound_routes.py`, `sound_project_routes.py`, `sound_registration.py` | Thin auth/idempotency routes and shared durable-job registration. |
| `pandrator_mcp/tools/sounds.py`, `project_sounds.py`, `sound_results.py`, `sound_audio_import.py` | Compact MCP projections and typed adapter operations. |
| `web/src/lib/Sound*`, `ProjectSoundControls`, `project-sounds-store.svelte.ts` | Parent-owned playground/library, cue chooser and bounded generation-view reads. |

Library/status reads use at most three SQL queries and do not decode or hash
audio. Native work and file checks happen outside SQLite writer transactions.
Library pages show 40 items, variant/candidate pages 20, and generation reads
cues for at most 100 visible segment IDs with at most 100 cues per page. Audio
players load lazily with `preload="none"`. Long-project cue reads do not load
the whole book or instantiate a player for every stored sound.
