# audio.cpp catalogue and speech support implementation — 2026-09-20

Implemented in `/home/lliniewicz/Projects/Pandrator_dev/Pandrator`. This is a local
working-tree change, not a deployment. Existing unrelated work was preserved.

## Scope and outcome

The pinned audio.cpp 0.8.1 inventory contains **86 families and 256 package
variants**, covering speech, transcription, conversion, music/sound generation,
analysis and audio tools. **117 speech packages across 36 families** are eligible
for managed installation. The app catalogue, Manager install registry and Manager
presentation agree exactly on those IDs.

Speech editing remains catalogue-only, including the two explicit DotTTS Edit
variants. Non-speech workflows are not implemented by this change. Gated or
unresolved downloads, multi-GGUF/special layouts, and routes requiring another
workflow are retained with a reason instead of being silently omitted.

The 15 reviewed manual package definitions retain their prior source pins. New
eligible packages have per-package immutable repository revisions and SHA-256
manifests. Model files were not downloaded during this work. The 224 verified
manifest rows include non-speech packages; manifest verification is distinct from
managed-install eligibility and acoustic validation.

## Discovery and selection

- New `/models` browser with ten starting points, search, task, capability,
  language and commercial-use filters, pagination, and package availability.
- Model details show languages, licence, reference recording/transcript needs,
  download size, upstream status, capabilities and actual Pandrator mappings.
- Recommendations include Qwen Base/CustomVoice/VoiceDesign, Breeze, Fish,
  Chatterbox Turbo, Pocket, Kokoro and Supertonic. They describe roles, not an
  acoustic quality ranking. Unknown licences stay explicitly unverified.
- Manager defaults to recommended and selected packages; search and a full-list
  toggle preserve selections while narrowing the display.
- Selected TTS models expose catalogue details. Voice design uses implemented
  model capabilities, languages and licences rather than a two-model allowlist.
- Broad unenumerated language claims remain qualified; OmniVoice design accepts
  a manually entered language code with a preview reminder.
- Read-only `GET /api/v1/services/audio-cpp/catalogue` and core MCP tool
  `pandrator_get_audio_cpp_catalogue` share filters and bounded pagination.
  Catalogue reads neither download models nor refresh/change the live service.
- Manager MCP status now declares when its compact model summary is truncated
  and points to the complete catalogue tool.

## Request translation

The shared performance compiler continues to preserve the spoken transcript
separately from provider control syntax. It reports unavailable controls.

Added/corrected mappings include:

- Turbo's native `max_new_tokens`, sampling controls and verified vocal events;
  no cloning or ignored CFG/exaggeration controls are advertised for this route.
- Supertonic preset voices, speaking rate, inference steps and seed. Unverified
  event tags were removed after checking the pinned implementation.
- Breeze's different English and Chinese event spellings.
- OmniVoice instructions and documented vocal events.
- VoxCPM2 parenthesized style prefixes with nested-control rejection.
- FireRed Instruct's design/reference templates. Reference-mode directions are
  reported unsupported because this runtime uses that slot for the transcript.
- CosyVoice3's explicit instruction template.
- NeuTTS preset `options.voice_id` and finite emotion choices.
- Irodori VoiceDesign instruction conditioning and nested language option.
- MOSS VoiceGenerator instructions, full language names in nested options, and
  preservation of 32-bit seeds through its signed integer parser.
- Pocket language belongs to each exact package.
- Unknown models no longer silently acquire cloning support; live explicit
  task/family metadata can resolve unambiguous aliases.

Safe scalar request descriptors are derived from native specs where available;
reviewed manual descriptors take precedence. References, file paths, templates,
editing controls and compound inputs are excluded from generic tuning. Not every
native feature has a Pandrator mapping: streaming consumption, audio insertion,
native multi-speaker requests and exact timing remain clearly separate. Installing
a package is not a claim of model-specific acoustic validation.

## Verification

- Integrated backend/API/MCP/provider/Manager regression selection: **328 passed**.
- After the final MOSS mapping, focused translation/provider selection:
  **125 passed**, including its new instruction/language/seed regression.
- Four Chromium checks passed: catalogue filters/pagination; mobile layout and
  API error recovery; capability-driven voice design; Manager filtering and the
  exact selected-package plan. Parent inspected desktop/mobile, design and
  installation renders.
- Frontend Svelte check: zero errors/warnings. Production build passed.
- Scoped Ruff and Pyright passed; focused Vulture check found no high-confidence
  dead code. Fresh-process forward/reverse import checks covered app and Manager
  catalogue integration (including the deliberately deferred Manager factory
  import), without partial-initialization failures.
- Built app and Manager wheels; both include inventory and curation JSON files.
  Data mirrors are byte-identical.
- Fresh MCP discovery tests assert the catalogue tool is in core and exposes its
  filter schema.
- Actual installed audio.cpp 0.8.1 `model_manager_v2.py` parsed copies of all
  **117 pinned package specs**, verifying repository, revision, target directory
  and stripped file paths without downloading anything.
- Terra's independent installer review found the two editing variants; these were
  excluded and regression-tested. Parent integration additionally corrected file
  layout pinning to the package-level fields consumed by the real downloader.
  The reviewer reported no remaining findings in its scope.

The existing Python `audioop` deprecation warning remains. These checks do not
certify every model on this GPU, real download success, voice quality or emotional
performance. No live services were restarted, no sessions changed, and no new
weights activated. A coordinated app/Manager/MCP rollout and representative
listening tests remain separate work.

## Provenance and refresh

The checked-in snapshot generator is `scripts/snapshot_audio_cpp_catalogue.py`.
It reads runtime specs, resolves public Hugging Face metadata and hashes bounded
small sidecars; it does not fetch weights. Both packaged JSON copies must be
regenerated together. Model-repository licence metadata is retained as an
unreviewed claim; it is not substituted for individual model licences.

Primary evidence:

- [Pinned audio.cpp model specifications](https://github.com/0xShug0/audio.cpp/tree/v0.8.1/model_specs)
- [Pinned speech documentation](https://github.com/0xShug0/audio.cpp/blob/v0.8.1/docs/tts.md)
- [Speech HTTP request mapping](https://github.com/0xShug0/audio.cpp/blob/v0.8.1/app/server/runtime.cpp)
- [Turbo request parser](https://github.com/0xShug0/audio.cpp/blob/v0.8.1/src/community_models/chatterbox_turbo/session.cpp)
- [Supertonic request parser](https://github.com/0xShug0/audio.cpp/blob/v0.8.1/src/models/supertonic/session.cpp)
- [MOSS design request parser](https://github.com/0xShug0/audio.cpp/blob/v0.8.1/src/community_models/moss_voicegen/session.cpp)
- Individual official model-card links are stored in `audio_cpp_curation.json`.

The snapshot retains the original model repository pin
`dc6fecccc2b0c6bdda0a8b2f38fa61394fee0b9c` for existing pinned specs. Public main
revisions resolve to immutable commits, including
`bd2f3e26c1a74fa359d712b4e46919fc244722c5` for current audio.cpp GGUF packages.

Specialists used: Luna researchers and bounded backend implementers; Terra
current-state researcher and read-only installer reviewer. Parent retained all
architecture, UI, integration and final auditing. No external OpenCode route.

## Local release verification

Release staging isolates this task from the separate, uncommitted voice UI work.
The frozen Manager specification now embeds both catalogue JSON files; a packaging
regression assertion covers them. On the isolated release tree, 503 backend tests
and four Chromium catalogue/installation cases passed; Svelte reported no errors
or warnings, and the production frontend build passed. Local deployment retains
previous slots and consistent database backups.
