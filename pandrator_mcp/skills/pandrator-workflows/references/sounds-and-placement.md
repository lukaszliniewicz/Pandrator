# Sounds, music and placement

Discover the current sound tools and schemas before use. A prior server build
may not expose them. Keep environmental sound effects separate from voices,
speech instructions and vocal-event tags.

For reusable audio, inspect `pandrator_sound_catalogue`, then
`pandrator_sound_library` with `action="list"` and a bounded query. Generate only
when the requested sound is unavailable or a new candidate is wanted. Use
`pandrator_generate_sound` for effects and music, then `pandrator_get_work`.
Use `pandrator_sound_catalogue` with `kind="music"` for discovery, then
`model_id` for the selected model's controls. Use those advertised parameters;
unsupported controls are rejected. The local routes include Stable Audio,
ControlFoley, MiDashengLM Gen, ACE-Step, HeartMuLa, MiniMax Music 3, YuE2 and H3
when their pinned packages and installed execution backend are available.
Supply `kind`, `backend`, optional `lyrics` and `parameters` only as supported.
Music 3 requires lyrics; Stable Audio has no separate lyrics input. YuE2 accepts
optional lyrics: empty lyrics are accepted but do not guarantee instrumental
output; the separate instrumental adapter is not installed by this package. Its description
is style conditioning and its duration is a maximum budget. H3 duration rounds
up to the native frame grid and generation remains audio-only. H3's experimental
INT8 variant requires CUDA; ordinary H3 CPU/Vulkan completion is unverified.
Use only the backends advertised for the selected model by the live catalogue.
Omitted duration uses the model default. CPU is the ordinary default and
generation is separate from the resident TTS model; first use installs only
the chosen complete, verified package, including required decoder components.
An omitted seed resolves randomly once within model bounds. Use a new
idempotency key and omit seed for another random candidate. Retry with the same
key and unchanged request to reuse its job and resolved seed. An explicit seed
allows repeatable settings and can reuse a matching cached candidate.

Use `candidate_count=2` through `8` for several takes of the same prompt/settings.
One base seed is resolved for the batch; following seeds increment and wrap in
the model's allowed range. The receipt lists compact candidate/job IDs and seeds
without repeating descriptions, lyrics or runtime provenance. Poll each pending
job, including a cached candidate whose job is still running. Retry an uncertain
admission with the same key and unchanged count/request to replay the whole
batch. A new key with an omitted seed creates fresh alternatives. Default count
one preserves the single-candidate contract. Save or discard takes individually;
active jobs and saved/project-pinned variants retain their existing guards.

Parameter descriptions carry model-specific meanings and starting values; read
them before changing advanced controls. Stable Audio Small/Medium are
post-trained checkpoints: guidance/negative prompt are accepted by audio.cpp,
but the vendor documents their useful effect for Base only. Start at eight
steps, guidance one and no negative prompt. ACE Base/SFT defaults to 32 steps
and guidance seven; Turbo defaults to eight steps and omits ignored guidance.
HeartMuLa codec duration is a decode chunk rather than song length, and its
positive controls advertise an exclusive zero boundary. H3's audio scheduler
control is `audio_flow_shift`; do not invent or pass undeclared parameters.

Save a ready candidate with `pandrator_sound_library`, `action="save"`. Supply a
name/tags for a new item, or `item_id` and `expected_revision` for a new variant
of an existing item. Existing cues retain their variant when the preferred
variant changes. Use `include_detail=true` only for one candidate/item when
prompt/runtime provenance is needed. For a recording, use the typed
`pandrator_import_audio_reference` operation with `target="sound"` and a
contained approved-root file. Discover root names; do not guess paths. The MCP
import limit is 16 MiB. Voice-reference imports use their separate target.

For placement, read `pandrator_project_sounds`, `action="get"` for the project
and use its current revision. Add/update pins a saved `variant_id`, an anchor
and playback settings. Segment anchors use exact IDs from the current speech
plan. Word anchors use the exact TTS-optimized spoken transcript when present,
quoted occurrence and optional Unicode code-point range. Use `after_word` for
a tight boundary, `next_pause` for a bounded later pause, or `source_time` for
timed placement. Never estimate a timestamp from character count.

Books default to insertion; later speech/chapters/captions shift together.
Use explicit `playback.mode="overlay"` for background music, with suitable
gain, fades and trimming. The UI defaults newly selected music clips to overlay.
Timed projects use overlay without extending source duration. Subtitle-only
projects keep effects as metadata. Changing effects does not require another
TTS run. Preserve existing source soundtrack/voiceover mix settings.

Resolve word placement with `action="resolve"`. Each job handles at most 20
cues; re-read the plan/revision after it finishes and continue only when the
receipt's `remaining_count` requires it. Current ready pins are skipped;
changed takes/transcripts are refreshed. Alignment is checked against waveform
quiet intervals, lexical guards and VAD. `no_safe_quiet_gap` means insertion
remains pending, preserving speech. Report the uncertainty; do not call it
success or silently exclude it. Explicit overlay is available when that suits
the user's intent. Placement status/reason is compact; internal alignment
transcripts and paths are not returned.

Use `action="render"` with the current revision and base assembly artifact.
Poll work, then inspect `action="status"` for the mixed artifact. Keep dry
speech and mixed IDs distinct. Outputs may explicitly disable `include_sounds`;
an included unresolved cue otherwise blocks the mix. Download the returned
current output using normal artifact tools and leave sound quality to the user.

Sound-plan records are canonical. XML markers derive from them and are stripped
before TTS: `<sound cue="UUID" variant="UUID" placement="after"/>`. Allowed
placements are `before`, `after`, `next_pause`; no paths, URLs or implicit
generation. XML preview/apply requires both speech-context and sound-plan
revision guards. Use project-cue tools for playback settings, timed anchors or
cues the segment XML cannot represent. `<event>` remains a speech vocal cue.
