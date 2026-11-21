# Contextual performance planning and pSSML v1

Pandrator can direct a deliberately short speech block using the meaning of its
surrounding text. It does not have to synthesize a long passage and transcribe or
cut the result. No previous generated recording is used as an implicit voice or
prosody reference.

## Workflow and interface

Select the intended **Speech plan** in the project overview. Its expandable
**Narration settings** section groups block length, sentence packing and dialogue
flow. The whole-sentence threshold controls Pandrator's packing of detected
sentences; it is separate from wtpsplit sentence detection. Short-dialogue
recognition defaults to on for new projects, uses punctuation/language rules
without an LLM, and preserves explicit Off overrides.

Open **Annotate & review**, or the **Speakers & delivery** project section, for
the annotation workspace. It is available for audiobooks and timed voiceovers.
The block navigator sits beside the editing view on wider screens. Select words
in the accepted spoken text, choose a character, unnamed dialogue or narrator,
and use **Mark selected text**. Select a complete marked turn to reassign it.
**Direct selected words** adds a phrase direction; whole-block controls remain
separate. The annotation preview highlights marked text. **Undo selection change**
undoes recent unsaved selection actions until another block control is edited;
**Discard block changes** restores the
loaded block. Neither is a history of already saved edits. Change spoken wording
in Generation rather than in the annotation workspace.

**Generation defaults**, **Speakers & cast voices**, and **AI & passive analysis**
are expandable sections. Analysis starts the configured LLM (or the model
specified in the form); preparing passive analysis creates work for a connected
MCP client without starting a model in Pandrator. A manual draft needs neither.
All three routes produce drafts for the same review and adoption workflow.
Active project work allows reading annotations but blocks editing and adoption.

General speech direction is independent of the optional analysis pass. For
Gemini, choose preceding text or preceding-and-following text to include a
bounded semantic window. The actual transcript remains the final labelled
section of the request. Prompt separation is not an enforced hidden channel:
listen for accidental context or instruction leakage before production use.

The optional performance pass runs after speech-block preparation. It reads the
accepted display text, accepted spoken text, nearby context, speaker, language,
block kind and available timing evidence. It returns **annotations, not edited
text**. Run it through the configured LLM, or create a manual draft. Configured
LLM analysis can incur that provider's costs; passive MCP analysis uses the host
model instead. Normal generation defaults remain unchanged until enabled.

Review the draft, choose a block, edit its direction, and preview the compiled
request without making an audio request. XML supports nested phrase controls and vocal events; legacy drafts retain
their JSON editor. A manual lock protects an annotation from reanalysis; changing
a locked annotation requires an explicit unlock. Adopt the saved draft when
ready. Unanalysed blocks can only be accepted unchanged explicitly. Adoption can
enable the plan for future generation but never overwrites an old take.

Adopted plans are immutable. Use **Copy for editing** for a new draft. Analysis
jobs checkpoint each validated batch and can resume unfinished work. A refresh
shows job state; **Analyse remaining blocks** uses the configured model. There is
no automatic adoption or audio generation at the end of analysis.

For audiobooks, use a stable narrator description and planning guidance about
the chapter/scene; paragraph context supports sparse local direction. This first
version uses the same optional post-plan pass rather than coupling performance
changes to spelling/pronunciation rewriting. It does not invent a full character
cast, automatically summarize the entire book, or add background effects.

## Representation

Annotations use `pandrator.performance/v1`. The authoritative schema is returned
with every leased batch and in the OpenAPI component definitions. For example:

```json
{
  "schema": "pandrator.performance/v1",
  "decision": "steer",
  "delivery": {
    "instruction": "Introduce a restrained contrast; become more assured toward the end.",
    "cadence": "continuing"
  },
  "spans": [
    {
      "anchor": {"quote": "temporary", "occurrence": 1},
      "delivery": {"emphasis": "moderate"}
    }
  ],
  "events": [],
  "reason": "The isolated sentence loses its contrast with the previous setback.",
  "confidence": "medium",
  "locked": true
}
```

`{"decision":"none"}` means no intervention and cannot hide delivery controls.
Directions are plain text, not native provider markup. Anchors must match the
accepted spoken representation exactly. Repeated phrases require an occurrence.
Overlapping spans and partial Unicode graphemes are rejected. This works with
Japanese and other scripts without relying on spaces or word indices. After a
later text change an adopted entry is judged per block: it is rebased onto the
new text only when the change lies outside its anchored words and every anchor
still resolves to the same words (or, for markup, the change stays inside one
stretch of it); anything else, and any topology change, needs review rather
than a silent remap.

A stale plan is not lost work. An adopted plan stays in use after an edit,
judged block by block (see below): only a block whose markup no longer fits,
or whose speaker, language, node kind or section changed, needs a new review.
That judgement keeps an unlocked neighbour of an edited block in use, though
the planner read the edited text as its context; copying the plan is the way
to have such neighbours reviewed again. Copying it (`copy_from_id`) onto its own
revision keeps every entry whose block is unchanged and whose planning context
(the neighbouring blocks the planner saw) is unchanged too; a locked entry needs
only its own block unchanged. An edited "said John" can change who speaks the
next line, so that line is reviewed again even though its own text did not
change. Manual copies keep every such entry; passive and LLM copies keep only
locked ones unless `keep_unchanged` is set, in which case only the changed
blocks are analysed. A stale plan copied onto a later revision (after a split,
say) first keeps the entries that still fit on its own revision, then maps
those across. Any copy onto a later revision maps only blocks that stay whole,
one block to one block, and an unlocked entry also needs its planning context
to read there as it did when the plan was made. The response reports `copied_count` (entries copied) and
`skipped_count` (source entries left behind); the plan's `total` minus its
`analysed_count` is what still needs review. Plans made before 0.14.5 do not
record a hash of each block, so for them a block counts as unchanged when its
recorded words, speaker, voice, language, structure and timing match.
Restoring a speech-plan revision, including the copy an edit makes of a
reviewed or used revision, carries that revision's adopted plan over unchanged,
because the restored blocks are exact copies. A plan already stale on its own
revision is carried stale in the same way: each entry moves with its block and
is judged on the new revision, so the carried plan reports the same changed and
review blocks, and the restore asks for a performance review only when one of
them needs it. On an edit copy the edit then makes the carried plan stale, and
only the edited block is judged again; a cast run is refused only when it
includes a block that needs review. A new plan made without `copy_from_id`
starts each block from the adopted plan's entry while that entry still holds
(unchanged or fitted), and passive and LLM plans keep those entries that are
locked.

Non-pause vocalizations are disabled by default. Enabling them permits explicit
requested events; it does not authorize the planner to invent emotional sounds.
A requested pause duration is only a synthesis hint, not a guaranteed assembly
time. The existing deterministic assembly silence controls remain separate.

## Model and backend capabilities

Capabilities belong to a model variant **and its backend route**, not to the
provider name alone. Inspect `expressive_capabilities` in the full TTS catalogue
or the performance-plan listing. `generation_prompt_models` remains a derived
compatibility field for older clients.

The initial compiler supports:

| Route | Directions | Phrase controls / context |
| --- | --- | --- |
| Fish S2 through audio.cpp or native Fish S2 | Inline free-form tags | Whole-segment cues recommended; phrase cues best effort, can be ignored or extend past the marked text |
| Drama 3 through audio.cpp | Inline free-form directions and Drama-specific vocal events | Paired phrase tags; cast voices render as separate parts; no native multi-speaker, previous-take or infill workflow |
| Qwen 1.7B CustomVoice through audio.cpp | Separate instruction | Phrase intent described in the whole-request instruction |
| Qwen 1.7B VoiceDesign through audio.cpp | Separate instruction | Stable voice description remains required |
| Kobold Qwen's instruction-capable prebuilt path | Separate instruction | Not applied to its cloning path |
| Gemini / Vertex 3.8 Flash and Flash Lite TTS | Separate speech metadata | Phrase intent and bounded context in metadata; angle-bracket vocal events |
| Gemini / Vertex 3.1 and 2.5 TTS | Prompt envelope | Phrase tags and bounded preceding/following semantic context |
| Supported OpenAI mini-TTS models | Separate instruction | Phrase intent approximated in the whole-request instruction |
| Breeze's audio.cpp instruction path | Separate instruction | Whole-request direction |
| ElevenLabs `eleven_v3` | Inline natural-language audio tags | Soft phrase directions and explicit supported vocal events; no request stitching |
| ElevenLabs `eleven_v4` | Inline natural-language audio tags | Native dialogue HTTP; up to 100 characters of before/after unspoken context |
| ElevenLabs `eleven_v4_turbo` | Inline natural-language audio tags | Complete realtime dialogue stream; stability control, no hidden context fields |
| ElevenLabs supported v2 / v2.5 models | No general direction field | Previous/next text in native stitching fields; no v3 audio tags |
| Native Chatterbox Turbo | Listed vocal-event tags | No claim of general instruction following |
| IndexTTS2/2.5 through audio.cpp | Native `options.emotion_text` | Whole-request emotion; required WAV reference; no general or phrase instructions |
| AuK Base/Flash through audio.cpp | Native `options.instruct` | Whole-request voice description; reference/duration and manual component prerequisites |
| MOSS-TTSD through audio.cpp | Native `options.instruct` | Request delivery; one positional `[S1]` speaker per rendered request |

Qwen Base/cloning and 0.6B CustomVoice do not inherit 1.7B instruction support.
Unrecognized variants/routes remain unknown. Voice design is distinct from
performance direction. Known event spellings are stored per model; Fish's open
instruction descriptions are not treated as a closed emotion enum. Other
providers retain their existing synthesis behavior; unsupported pSSML is reported
rather than inserted as ordinary speech. This is an extensible capability table,
not a claim that every local model or backend feature is implemented.

A compiled request reports `applied`, `approximated`, `unsupported`, and
`disabled` controls. "Applied" means encoded using that route, not acoustically
verified. Model compliance, tag scope, emotion and timing remain probabilistic.
No additional synthesis block is created to emulate an unsupported phrase control.
When Fish controls are compiled for audio.cpp, the request also selects its
`tag_aware` internal chunking mode. This safeguard overrides an incompatible raw
chunk-mode preference for that directed request and is visible in the compiled
request options. Undirected requests retain their existing chunk mode.

Scope is resolved from model **capabilities**: `native_instruction_scope`,
`phrase_instruction_transport`, `phrase_instruction_reliability`,
`recommended_instruction_scope`, `semantic_context_transport`, and `instruction_transport`.
`quoted_hint` quotes a phrase in the whole-request instruction; `inline_hint`
inserts a cue before its text; `none` retains and omits an unsupported phrase
direction. Neither hint guarantees a phrase boundary. A uniform root direction
is emitted once; adjacent explicit directions replace automatic restoration cues.
Internal XML is never passed directly to the provider.

Eleven v4 uses contextual `[pause]`/`[long pause]` tags without fixed duration.
Verified legacy Multilingual v2 and Flash v2/v2.5 routes use SSML breaks capped
at three seconds. Unverified Turbo/v3 pause parity remains unclaimed.
Azure OpenAI deployment names need explicit `model_catalog[].upstream_model_id`
before native model capabilities apply. Compatible JSON alone does not establish identity.

See [model identities and speech controls](reference/model-capabilities.md)
for the controls supported by each serving route.

## Reproducibility and review boundaries

Generation snapshots include the adopted annotations and immutable semantic
source text. Selected-block regeneration still gets its neighbours from the
whole accepted plan, not merely the selected generation batch. Neighbour text is
stored once per run; context is bounded and section/language boundaries are
respected. Another speaker's text can supply semantic evidence (for example a
question before a short answer), but is labelled and never selects the voice.

Alignment, verification, subtitle text and spoken-length calculations use the
clean transcript, not Fish tags or Gemini's prompt envelope. Effective compiled
requests contribute to audio identity. Changing a reason/confidence/lock alone
does not stale audio, while changed applied delivery or supplied context does.
History remains inspectable when a performance plan becomes stale. An adopted
plan is judged block by block after an edit: unchanged blocks keep their
directions, and so does an edited block whose markup still fits its new text
(the change lies inside one stretch of it). A block whose markup no longer fits
needs a new speaker plan review, and so does a block whose speaker, language,
node kind or section changed. A plan made before Pandrator recorded those fields
for each block cannot show they are unchanged, so after any change to the
revision, a voice or pause change included, every block of it needs review, as
the whole plan did before per-block judgement, and so it does in a project copy.
An edit does not fit when it reaches the start or end of a
spoken line or of a stretch with its own voice, where it could be the narration
around it, or any boundary inside one, such as an event, a delivery control or a
nested span; when it adds text right beside an event or delivery control, by
inserting it, by lengthening the words there or by adding words to the words
that touch it ("Hello" to "Hi, hello" or "Oh hi"), where it could fall on either
side; or when it adds, removes or replaces a quotation mark anywhere in the
block, with or without a spoken line, in either annotation format, since
narration that gains one may now hold dialogue. A change whose span merely
includes an existing quotation mark counts too: two edits on either side of one,
saved together, form one change across it. A straight or curly single quote
counts unless letters stand on both sides of it, as in "didn't", so an edit that
moves it into or out of a word counts too; one that stays inside a word
("didn't" to "couldn't") still fits. Letters of a script written without spaces
between words, such as Chinese, Japanese or Thai, do not make it an apostrophe.
A dash (em dash, en dash or horizontal bar) counts as a quotation mark where it
opens a line, with only whitespace before it on its line, as a line of dialogue
opens in Polish, Spanish, Russian or French ("— Idę do domu — powiedziała.");
so does an edit that adds or removes the text before such a dash on its line,
while a dash inside narration is an ordinary edit. The backtick of older e-texts
(`like this') and the heavy quotation mark ornaments count as quotation marks;
primes, the acute accent and the Hebrew gershayim do not. An edited attribution ("said John" to "said
Mary"), or an attribution written into a spoken line without quotation marks,
still fits and keeps the speaker the plan chose; copy the plan to have that
block reviewed again. A generation run that includes a block needing review is
refused with that block named; a run without it can still start, and the block
preview shows the review message for it, as does a preview against a run that
left it out. Recasting, workflow checks and voiceover regrouping or repair
cannot leave a block out, so they are refused while any block of the revision
needs review. A plan read reports `changed_segment_count`,
`review_segment_count` and `review_segments` (the first 20, each with its
0-based `ordinal`; the refusal and the speaker plan panel number blocks from 1)
for an adopted plan on the selected revision; `stale` keeps its meaning. Copying
the project carries such a plan into the new project, where the blocks that need
review still need it. Once an edit has made it stale, every block of a plan made
before Pandrator recorded each block's speaker, language, node kind and section
needs review there, as in the original: the copy rebuilds each block's plan entry
from the current block, so an old entry kept there would hide a change to a field
the plan never recorded (a block whose speaker changed would keep its old markup
and look current). Such a plan that is still up to date cannot be copied, as
before. An
edited block whose markup
still fits arrives there on its new text and counts as unchanged, and a plan
with no block left to review arrives up to date (`stale` is false there, while
the original's stays true). So a speaker plan copy in the new project keeps that
block's entry and its neighbours' entries. To have the block reviewed again,
copy the speaker plan in the original project, then review and adopt that copy,
before copying the project or in the new project afterwards: the copy travels as
a draft beside the adopted plan, whose entry is used until the copy is adopted.
Phrase editing, the starting markup of a split, merge or re-segmentation, and
the plan's own segment preview still need a plan that fits the whole revision:
for those, copy the stale speaker plan and adopt that copy.

With casting on and performance off, a revision without an adopted plan voices
each block from its own markup, which usually means the narrator reads the
dialogue. When an earlier revision on the same lineage adopted a plan (for
example before a split, merge, re-segmentation or batch edit), generation is
refused and names that plan, so it can be copied and adopted first. The start
preview reports it as `missing_speaker_plan`, the plan list as
`ancestor_adopted_plan`, and a caller that really wants the block markup passes
`allow_without_speaker_plan`.

The workflow audio stage (reviewable and direct audiobook audio jobs), its
preflight when the request names the revision, and recast refuse such a
revision too, with a rule per block, because these paths can run from a source
that carries its own speech (a reviewed speech-optimized narration, subtitles
with speaker markup, or diarised speaker labels). Without the plan, a block is
still voiced from its own speech markup, its block voice or its speaker label
(cast through the source speakers, or as dialogue); only a block with none of
them falls back to the narrator. So these paths refuse only when a spoken block
(not removed, with spoken text) has none of them, and name those blocks; a
revision whose spoken blocks all have one runs from them. A block whose only
voice is a saved project speech reference counts as having none, so these
paths refuse it although that reference would voice it. The recast preview is blocked with that message and
recast apply refuses with it. Plan edits made on top of a block's own speech
are not used on these paths. None of them has an acknowledgement, so a revision
the generation start ran with "Generate without the speaker plan"
(`allow_without_speaker_plan` for API and MCP callers) still cannot be run by
the workflow stage or recast while a spoken block would fall back to the
narrator: copy and adopt the named plan, or generate from the generation start.
A workflow stage that takes its input from a new source stores that source's
revision first, so a refused source leaves its revision active, beside the
adopted one. That revision has no edit lineage, so copying the plan to it
(`copy_from_id`) carries no entries: the copy needs analysis and review before
it is adopted. A retry from the same source reuses the revision.

A direct dubbing audio job is not checked. It registers new speech blocks on
every run and stores them as a new revision, so a copied plan or an
acknowledgement has nowhere to live before the job voices it. It runs as it
does without this guard: with casting on, a block without its own speech
markup, block voice or speaker label is voiced by the narrator, whether or not
an earlier revision had a speaker plan. This is a known limit.

With performance on, the "no adopted performance plan" error names the earlier
revision's plan too, except on a revision a direct dubbing job stored, where it
keeps its original text because a copied plan cannot help that job.

Automatic post-generation split repair can use read-only semantic context,
including configurable preceding and following blocks (for example one of
each, or the wider supported context windows). The global defaults remain two
preceding and one following block (`performance_context_before=2`,
`performance_context_after=1`); a session or generation can explicitly save one
of each, and repair uses those frozen values rather than re-deriving them.
When a split is staged,
Pandrator freezes a fresh context window for each new child and regenerates
neighbouring takes whose semantic windows changed in either direction. A
refreshed neighbour that is longer than the take it replaces rejects that block's
repair; raw take duration is compared with no tolerance, so even a small growth is
refused rather than assuming cue slack. Other blocks can still be repaired.
The original takes and plan remain available. Automatic regroup is still
disabled with semantic context. A voiceover generated with casting or
performance directions is not repaired or regrouped at all, automatically or
on request (`repair-timing` answers `409 invalid_state`), because its speaker
plan is reviewed block by block: split the block in the speech plan, review the
speaker plan and regenerate instead.

## MCP and API

All operations are session-scoped. Reads require `app.read`; edits/adoption use
`app.write`; analysis and leased batches use `app.run`. Mutations require an
idempotency key. There are no arbitrary file paths, URLs or provider credentials
in performance tool arguments.

For passive analysis:

1. Inspect the selected speech-plan revision and full model capabilities.
2. Call `pandrator_create_performance_plan` with `mode="passive"` and the exact
   `expected_plan_revision_id`.
3. Claim with `pandrator_claim_performance_batch`. Follow its schema, actionable
   IDs and read-only context; return every ID exactly once and in order.
4. Submit with `pandrator_submit_performance_batch`, retaining the lease token.
   Renew or release the lease when needed. Completed results are checkpointed;
   an expired lease must be reclaimed.
5. Inspect with `pandrator_get_performance_plan`, preview with
   `pandrator_preview_performance_plan`, and use guarded edits where needed.
6. Adopt with `pandrator_adopt_performance_plan` and the inspected version.

Configured-model analysis uses `mode="llm"` or
`pandrator_analyse_performance_plan`. The returned job reference uses the existing
work-status and cancellation tools. The UI and MCP share the same validation,
adoption and compilation services.

HTTP resources are under
`/api/v1/sessions/{sessionId}/performance-plans`. Request schemas, permissions and
idempotency requirements are included in the generated OpenAPI document.

## Validation boundary

Automated tests cover schema/anchor correctness, request compilation, real
in-memory MCP transport, migrations on disposable databases, leases, replay,
manual lock precedence, interruption/resume, source staleness, immutable
regeneration snapshots and provider/generation regressions. These tests do not
prove the acoustic quality of a model's interpretation. Compare representative
short utterances with and without direction and listen before enabling this
across a production recording.

## Gemini and ElevenLabs setup

Gemini 3.8 Flash and Flash Lite TTS keep the spoken transcript separate from delivery directions
in `speech_metadata.style` (Cloud: `speechMetadata.style`). Optional preceding and
following text stays in that metadata, with explicit unspoken context labels.
Phrase directions are soft whole-request hints; supported vocal events use
angle-bracket tags such as `<laugh>` and `<short pause>`. Unary WAV output is
decoded directly without adding a second WAV header. The existing 3.1/2.5 models
retain their prompt-based path.

Gemini 3.1 and 2.5 combine general direction, labelled preceding/following context, and the
current transcript in one prompt. Pandrator keeps the transcript last and uses
accepted neighbouring speech blocks, including when regenerating only a selected
block. Context is text, not previously generated audio. Inline tags and prompt
separation remain model-interpreted; they do not guarantee an inaudible hidden
channel or exact phrase timing. See [Google's TTS prompting guide](https://ai.google.dev/gemini-api/docs/speech-generation).

ElevenLabs `eleven_v3`, `eleven_v4` and `eleven_v4_turbo` receive compiled directions and supported events inside
`text`; there is no separate `instructions` parameter. Phrase restoration is a
soft direction. The currently mapped vocal events are laugh, sigh, and throat
clearing, gated by `performance_allow_vocalizations`. Other events are reported
as unsupported. These are vocal performances, not a background-effects mixing
workflow. See [ElevenLabs audio tags](https://elevenlabs.io/docs/overview/capabilities/text-to-dialogue).

For supported older ElevenLabs models, preceding/following context uses
`previous_text` and `next_text`. Eleven v3 does not support this stitching path.
Unknown models receive no inferred expressive capabilities. See
[ElevenLabs request stitching](https://elevenlabs.io/docs/eleven-api/guides/how-to/text-to-speech/request-stitching).

Eleven v4 uses Text-to-Dialogue HTTP and maps `similarity_boost` to the native
`settings.similarity` field. The Turbo WebSocket route exposes only `stability`.
For these routes, unsupported controls are rejected. v4 trims optional context
to the nearest 100 characters on each side and sends `previous_text`/`future_text`;
Turbo does not send hidden context. Both routes cap compiled input at 2,000
characters. A stream must complete successfully before its audio is saved.

Optional `elevenlabs_voice_settings` is an object. An empty object keeps provider
defaults. Older-model REST requests can use `stability`, `similarity_boost`, and `style` from 0 to
1, `speed` from 0.25 to 4, and boolean `use_speaker_boost`. Invalid types,
ranges, and unknown keys are rejected before the HTTP request. Provider
documentation disagrees on non-stability settings for Eleven v3; Pandrator
forwards those REST options but marks their effect as approximate because the
model may ignore them. See the [ElevenLabs REST settings reference](https://github.com/elevenlabs/skills/blob/main/text-to-speech/references/voice-settings.md).
Voice settings, compiled directions, and supplied context affect whether
existing audio can be reused.

The catalogue distinguishes request-level directions from phrase hints. FireRed
Instruct can take directions without a reference; its reference mode uses that
slot for the reference transcript. Azure style choices depend on the selected
voice and use `azure_speech_style`, not general pSSML emotion annotations. Sound
and music models marked **Catalogue only** have no generation workflow in this
release.
