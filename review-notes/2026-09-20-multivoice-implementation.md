# Multivoice follow-up implementation

Implemented and verified in the development checkout on 2026-09-20, based on
`e23e19be748bce083d1e19859a61b00fed68f896`. This records the implementation of
the accepted proposal in `2026-09-20-multivoice-trial-follow-up.md`.

## Delivered behavior

- Narration preprocessing recognizes punctuation before closing quotation marks,
  preserves source paragraph boundaries and spaces at dialogue joins, and protects
  leading-apostrophe words from destructive NeMo normalization. The existing
  wtpsplit splitter remains in place. The real Christmas Carol CPU reproduction
  confirms the reported quote and spacing defects are gone; see
  `../experiments/2026-09-20-multivoice-segmentation/AFTER.md`.
- Audiobook split and merge preserve validated character/narrator spans, delivery
  markup and boundary semantics. A bounded `resegment` action accepts automatic
  grouping or explicit Unicode character offsets and creates an inactive draft.
  Adoption is revision/signature fenced. The draft cannot be selected or generated
  accidentally. Changed blocks require regeneration; unchanged lineage can retain
  takes and reviewed annotations only when text and structural settings agree.
  Cuts that would discard vocal events are rejected.
- Continuation and other semantic boundaries are frozen into generation/assembly
  snapshots. Both explicit assembly and export's inline assembly consume those
  pauses. Adopting performance changes invalidates current-selection assemblies;
  historical run snapshots remain reproducible. Punctuation-only spans no longer
  become separate synthesis requests.
- MCP model management now accepts bounded audio.cpp `models` and `add_models`
  lists through the existing Manager plan/execute lifecycle. `add_models` preserves
  installed and persisted desired models. The plan displays the canonical model
  set, additions/removals/retentions, size estimates and restart tasks. Download,
  verification, activation and rollback remain owned by Manager. Completing an
  operation refreshes the live catalogue when the app is available.
- Model maintenance and application work cannot start over each other. Tests cover
  both transaction orderings and failed resume preserving paused state. Explicit
  operator stop/restart commands remain outside this model-operation guard.
- Preview distinguishes compilation, model availability and untested acoustic
  compliance; reports its source revision and effective assembly pause; and
  retains cast settings during a service override. CustomVoice/VoiceDesign routes
  reject incompatible cloned references rather than presenting a usable preview.

The MCP guides document the contracts and limits. No UI surface was changed.

## Verification

- Earlier broad regression selection: **210 passed**.
- Latest five-suite audio/resegmentation/performance/cast/workflow run: 144 passed
  and one obsolete test assertion failed. The mock optimizes the first paragraph;
  the assertion selected the newest-created segment, which is now the second
  paragraph because source paragraphs are preserved. Inspection confirmed both
  active segments were correct and completed. Updated the assertion to inspect
  the active plan in ordinal order; the full workflow suite then passed **71/71**.
- Latest MCP, Manager, maintenance and refresh selection: **74 passed**, including
  fresh-process tool discovery and additive-model JSON-schema acceptance.
- Independent Terra backend review: no remaining findings; **110 scoped tests
  passed**. Its annotation-copy finding was fixed and rechecked before completion.
- Real WAV tests prove that a continuation overrides a stored 700 ms pause in both
  explicit assembly and inline export: 100 ms + 140 ms inputs produce 240 ms.
- Ruff checked 40 changed/new Python files: **zero introduced diagnostics** and
  five existing diagnostics in the workflow handler and its older tests.
- Pyright reports no errors in the five new modules and changed Manager planner.
  The checked integration files retain the same 21 diagnostics as HEAD, with no
  additions. Vulture at confidence 100 found no issues in the five new modules;
  the local top-level import scan found no cycles involving them.
- `git diff --check` passed.

Counts overlap and must not be summed as a unique test total. Mocked lifecycle
tests do not constitute a live multi-gigabyte model download/activation trial.

## Live expressive-route smoke test

The already installed Breeze TTS 2 model was used through the existing live MCP
for two short auditions. These exercised the existing deployed provider route,
not the new checkout. No new model package was downloaded.

| Character | Reference voice | Text | Direction | Audio | Job wall time |
| --- | --- | --- | --- | ---: | ---: |
| Scrooge | `pandrator-pandrator-sample-voice-pandrators` | Bah! Humbug! What reason have you to be merry? You're poor enough. | Speak in a clipped, dismissive, irritated tone. Keep the words clear and the pace deliberate. | 5.28 s | 47.00 s, cold |
| Fred | `pandrator-voiceover-design-acdb83e145` | Don't be cross, uncle! Come! Dine with us tomorrow. | Speak warmly, brightly, and with genuine delight. Invite your uncle cheerfully. | 4.72 s | 10.31 s, warm |

Both use `audio_cpp`, `breeze_tts_2_q8_0`, English, seed 42, and produced non-silent
24 kHz mono WAVs. Stored metadata confirms the requested model, reference voice,
prompt and seed. They were combined with a 250 ms gap at:

`/home/lliniewicz/exports/a-christmas-carol-breeze-followup-20260920.wav`

The combined sample is 10.25 seconds. This establishes successful synthesis, not
audible emotional compliance or cross-engine identity fidelity. Listening quality,
transcription accuracy and peak GPU memory were not measured.

Audition job IDs: `051b3924-ce72-4ba0-9281-771884acc36b` and
`776dad70-09b4-445f-8358-c1c80fb38384`.

## Rollout and remaining scope

The original Christmas Carol session, its cast, and its 16 completed takes were
not edited. The auditions created two separate preview artifacts and loaded
Breeze in the existing single-model runtime.

The checkout has not been committed, pushed, installed into the managed runtime,
or deployed. App, Manager and long-lived MCP processes still need a coordinated
rollout/reconnect before they expose these changes. Fresh checkout MCP processes
were used for discovery verification. After rollout, use one bounded model
operation and a draft repair of the existing session for live acceptance.

Replacing wtpsplit, generating a whole novel, benchmarking every expressive
engine, and promising emotional delivery remain outside this phase.

Specialists used: Luna/xhigh researchers and implementers for bounded backend
contracts, implementation and reproduction; Terra/xhigh for independent backend
review. The parent integrated and audited the changes. Live provider checks used
Pandrator MCP. No external OpenCode research route was used.
