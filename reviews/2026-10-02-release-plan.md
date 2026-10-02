# Proposed next-release plan — 2 October 2026

This plan combines the [release audit](2026-10-02-pre-release.md), the related wishlist items, and the requested language/ASR/preprocessing changes. It is an implementation plan; product code has not been changed by the audit.

**Proposed release scope:** restore the broken contracts; track language support per exact model/provider/operation; prefer Parakeet with safe language-aware ASR fallback; offer Demucs vocal isolation; complete subtitle editing and multilingual coordination. Each phase has its own acceptance gate. A broad application rewrite and indiscriminate exposure of every audio.cpp task are outside this scope.

## Sequence

| Phase | Deliverable | Dependency / exit condition |
| --- | --- | --- |
| 1 | Fix confirmed product defects and restore test gates | Default settings save, isolated MCP startup, review, voice and purge regressions covered; lane/lint/collection/version expectations fixed |
| 2 | Reliable model/operation language metadata and expanded pickers | Exact package/provider support, aliases and provenance agree across catalogue, request boundary, HTTP, UI and MCP |
| 3 | Parakeet-preferred ASR routing and Demucs preprocessing | Language/timing/availability preflight is explicit; native separation cancellation, duration and memory checks pass |
| 4 | Coherent subtitle editing and draft protection | Passage/display actions make sense before save; drafts survive failures and closing is deliberate |
| 5 | Multilingual project readiness, selected-language operations and export collection | Partial completion/retry/conflict behavior is durable and reviewed inputs stay pinned |
| 6 | Final packaged, migration, browser and managed-runtime acceptance | Fresh artifacts and all required CI pass; later release/activation uses the verified artifacts |

Implement phases as reviewable changes with focused checks, then integrate. Phase 1 is the immediate decision-complete repair packet. The later phases below define behavior and invariants; any discovered missing backend primitive must be resolved before assigning an implementation packet for that phase.

## Phase 1 — Restore the release contracts

### Settings and independent MCP package

- In `SessionWorkspace.svelte`, make `stageSectionUpdates` emit STT fields only to `stt` and composition fields only to `subtitles`. Remove the extra `original_language` alias from new STT writes; persist the source-language choice through its canonical field.
- In `settings_policy.py` / `workspace_settings.py`, normalize recognized legacy STT language and subtitle aliases into canonical fields/sections. Define precedence: an explicit canonical value wins over its legacy alias; conflicting explicit inputs receive a useful validation error. Do not erase unknown stored data silently or relax credential restrictions. Re-saving an old session must preserve its intended choices.
- In `pandrator_mcp/schemas/sessions.py`, define an MCP-owned multilingual input DTO with the current wire fields and constraints. Have `server.py` import that DTO, remove application imports, and protect equivalence with backend/OpenAPI contract tests. Keep backend validation authoritative for writes.
- Acceptance: untouched and edited Transcribe settings save/reopen; legacy regional language/custom subtitle overrides survive; invalid enums/unknown new keys/credentials still fail. An installed MCP wheel must import its schemas and server and exercise CLI help/tool registration without application packages or SQLAlchemy.

### Canonical reviews, voices and purge

- Establish one small, dependency-safe role-to-document-stage mapping for the review/logical-passage boundary; apply it to the new TTS review guard. Preserve revision, session and content-hash ownership checks.
- In `subtitle_review.py`, use one collision-free output-passage identity policy with explicit source ancestry. Reconcile speaker edits on reflowed fragments into the canonical passage. Mixed fragment speakers require deliberate resolution rather than silently selecting one.
- In `generation_cast_runtime.py`, enforce the complete session voice identity unconditionally at the final strict-single-voice boundary. Keep stored cast/segment/alternate choices inactive for later use. Align worker requests with preview and audio identity.
- In `session_purge.py`, include owned unfinished uploads in the impact manifest and journal. Recheck state under the purge transaction; cancel/block an active writer safely, remove its managed storage before losing the identifying record, and support retry after partial filesystem failure. Preserve shared sources and other sessions' uploads.
- Acceptance: unchanged TTS review save/reopen; deletion around a reflowed passage; text/speaker edits; split/merge ancestry; single-voice plain and alternate overrides with batch sizes 1 and greater than 1; switching back to multiple voices; purge preview bytes, pending chunks, interruption/retry and path/symlink guards.

### Release gates

Register the six missing Python test files exactly once. Rename `VoiceSetupTests` to a collected class and check the default collection count. Repair the two Ruff failures. Refresh audio.cpp version expectations only after verifying pinned asset names/digests. Update renamed browser selectors/copy while retaining behavioral assertions. Restore the source-passage conflict test after the real STT save fix.

Gate: focused regressions pass, normal test collection includes the new coverage, lane check passes, quality passes, and Linux/Windows CI runs the actual tests.

## Phase 2 — Language coverage that follows the exact model and operation

The current 50-language picker is insufficient, and family-wide metadata is sometimes wrong. The detailed evidence is in the [language and preprocessing audit](2026-10-02-language-and-preprocessing-audit.md).

### Capability contract

Use a versioned capability record keyed by **provider, model/package, revision, operation and native route**. Keep separate coverage for recognition, word alignment, TTS and voice design. Separation/enhancement tasks are language-independent operations, not ASR/TTS models.

Each language record must distinguish:

- Canonical language/locale tags, provider request aliases, and voice/sample language metadata.
- An exact documented support list, a documented subset, a broad multilingual claim without an enumerated list, and unknown coverage. Empty or unknown coverage must never mean “all languages” or “English only.”
- Upstream checkpoint claims versus the selected native adapter's actual support. A newer model card does not expand an older pinned package automatically.
- Provenance URL, source revision/date, catalogue revision, runtime requirement, and whether metadata was discovered from the running provider or supplied offline.

Store prose such as “80+ languages” in a coverage note, never in a machine-readable language array. Expand the language registry using exact documented lists, including Fish’s 83-code metadata and OmniVoice’s 646-entry language map, and reviewed name/code mappings where necessary; retain valid regional/script tags. Searchable labels and stable codes must work for less common languages and imported sessions. Keep common languages easy to find without rendering hundreds of checkbox rows at once.

### Required corrections and UI behavior

- Curate SanoTTS per language package; distinguish IndexTTS2 from 2.5; review unverified VoxCPM1 additions. Refresh the 0.8.1 inventories against the pinned 0.9.0 runtime, tracking weights separately from binary versions.
- Supply exact static Silero pack languages and provider aliases, then merge live discovery with clear provenance. Distinguish CIS Base/Extended/Russian/native Indic/Indian-English packs, their voices, and stress requirements. Do not infer native-language support from an Indian-English voice name.
- Retain model-specific cloud-provider language metadata and route limits. Do not substitute XTTS's language list for Fish S2 support.
- Fix base-language/locale matching in catalogue filters without erasing provider-specific regional distinctions. Align preflight, subtitle evidence and request normalization; include Azure Norwegian `nb`.
- Replace the global picker as the source of capability truth. Source-language selection follows ASR coverage; speech selection follows the selected TTS model/voice; translation project targets use the language registry and show TTS readiness independently. A subtitles-only project must remain usable when no TTS model supports its target.
- Known unsupported speech choices receive a clear reason before a job starts. Unknown coverage stays visibly unverified, with an explicit language entry path rather than a fabricated supported list. Existing saved values remain visible and repairable.

Surfaces: `audio_cpp_inventory.json`, `audio_cpp_curation.json`, `audio_cpp_catalogue.py`, `model_catalogue.py`, provider descriptors/adapters, `stt_languages.py`, `subtitle_evidence.py`, HTTP/OpenAPI/MCP projections; parent-owned `settings-fields.ts`, `LanguagePicker.svelte`, `voice-catalog.ts`, provider/model details, workspace/settings/generation voice controls and the wizard.

Gate: for each documented model variant, every advertised language is selectable in its relevant UI and every excluded language produces the same result in catalogue, preflight and request compilation. Cover Swahili/Burmese/Khmer/Lao, CIS and Indic languages, `pt-BR`, `en-GB`, `uk-UA`, `nb`, unknown custom-model coverage, and TTS/ASR lists with deliberately different sets. Validate the complete enabled catalogue mechanically; use representative real-model smoke checks for uncertain route/locale behavior rather than claiming acoustic quality for every language.

## Phase 3 — Parakeet-preferred ASR and optional Demucs

### Default routing policy

New/inherited ASR settings should use **Automatic · Parakeet preferred**. Preserve explicit existing selections; selecting a model manually must remain an override.

Automatic timed transcription resolves in this order:

1. **Parakeet TDT 0.6B v3** when the resolved language is in its 25-language set and its runtime is usable.
2. **Qwen3 ASR** when Parakeet cannot cover the language or is unavailable, Qwen's runtime meets its requirement, and both recognition and a configured word-alignment route support the language.
3. **Whisper** as the supported timed safety fallback when the preferred routes cannot satisfy the request. Explain the reason. If no supported installed runtime can do the job, show the required installation/selection instead of submitting a doomed job.

Qwen currently adds Chinese, Cantonese, Japanese and Korean beyond Parakeet for timestamp-required workflows. It recognizes additional languages such as Arabic and Thai, but those lack the current timed path. Never call a recognizer-only language “unsupported ASR”; identify the missing timestamp/alignment capability.

For automatic source language, resolve language before routing. For the first implementation, use the pinned CrispASR 0.8.40 multilingual Whisper Tiny detector-only CLI on a bounded, normalized sample of at most 15 seconds. Pin/hash-verify its model separately, use the owned cancellable runner, and parse one validated language/confidence diagnostic. Use a conservative recorded confidence threshold, validated with retained clips before release; explicit language bypasses detection. If language detection is unavailable, unknown or contradictory, use Whisper when it is usable and disclose the reason; otherwise request an explicit source language. Do not optimistically run Parakeet on an unknown language and treat an arbitrary result as proof of support. Freeze detected/declared language, chosen engine/model, alignment route and fallback reason in job provenance.

Availability has two states: runtime usable and weights cached. Existing first-use acquisition remains supported, but the preflight must show needed ASR/alignment downloads and their sizes. Installing audio.cpp is not proof that CrispASR/Qwen transcription is ready. Never switch away from an explicit engine after a recognition failure without a user-visible, recorded decision.

Surfaces: `settings_policy.py`, `stt_backends.py`, `crispasr.py`, `transcription.py`, `qwen_asr.py`, runtime capabilities, session/Quick Transcribe/voice-reference entry points and MCP; parent-owned model choices, route explanation and source-language controls.

Gate: English/Polish → Parakeet; Japanese/Chinese → Qwen with alignment; Arabic/Thai → Whisper under today's timed capabilities; missing/old Qwen runtime; missing weights versus missing executable; automatic/unknown language; cancellation and detection failure; explicit Whisper/Qwen preserved; settings aliases; consistent session, quick and reference-sample behavior. Stub provider tests establish routing; retained real short clips establish language/timing acceptance before release.

### Untimed transcript companion

Make timestamp requirements explicit in the request contract. For TXT-only requests, use the same preferred routing order with recognizer coverage as the eligibility condition; Qwen can cover its full recognizer language set without an aligner. Timed subtitles/evidence still require an aligned route. Produce an explicitly untimed text result without fabricated cue/word times, and prevent it from being advertised as a timed source. Test Arabic TXT using Qwen with no aligner acquisition, then verify that SRT and timing-dependent workflows still follow the timed fallback rules.

### Demucs preprocessing

Add **HTDemucs four-stem Q8** as an explicit vocal-isolation option at the existing `apply_vocal_isolation` junction. Keep preprocessing off by default. Four stems already exist in the pinned model inventory and include the required vocals output. Six-stem HTDemucs is the new 0.9.0 addition; catalogue it accurately, but six stems are unnecessary for the initial vocal-isolation option.

Reuse native audio.cpp separation, hash-verified managed weights, 44.1 kHz normalization, the owned cancellable subprocess, watchdog/resource guard, validated vocals output, and derivative provenance. Preserve the original media and timeline; convert the validated derivative for ASR/alignment using the existing path. Failed separation must be explicit. Do not silently revert to original audio and pretend preprocessing succeeded.

Expose the same selection in session transcription, Quick Transcribe, attached-caption alignment and MCP. Record package/revision/backend/settings and processing artifact identity in job results. Display honest stage progress and cancel state; do not invent per-chunk percentages that the native runtime does not provide.

Update `audio_cpp_processing.py`, pinned processing assets, `qwen_asr.py`, quick-transcription schemas, parameter definitions and capability projections together. Parent owns the preprocessing picker, first-use download/memory explanation and error/cancel UX.

Verified benefit: smaller model download, approximately 62 MB for four-stem Q8 versus 173/252 MB for the existing RoFormer Q8 choices. **Lower compute/peak-memory use is a hypothesis to test**, not release copy. Native Demucs retains whole-track/stem buffers, so include a long-input check.

Before describing it as lighter, benchmark the same retained input/backend/thread settings for four-stem Q8, BS-RoFormer Q8 and Mel-Band Q8. Record cold load separately from processing time, real-time factor, peak RSS/VRAM, output duration and obvious speech loss/artifacts. Use one short mixed speech/music clip and one longer recording; retain provenance/checksums and make candidate/input settings fixed before execution. Stop after two successful runs per candidate/input or one repeatable failure; expand only for conflicting results. If Demucs is not faster or lower-memory on the target backend, retain it as an alternative without that claim.

Gate: CLI/model compatibility on supported CPU and one GPU path, correct vocals selection, finite/nonempty output, existing absolute duration tolerance, source hash unchanged, owned-process cancellation, retry/cleanup, safe long-input behavior and the same preflight through HTTP/MCP/UI.

## Phase 4 — Editing that matches the canonical speech model

### Passage and display-cue controls

Return edit capabilities and ownership context from the review service. The UI should explain when several display cues belong to one spoken passage. Offer text, timing, speaker and utterance actions at the level that can safely persist them. For a turn split, use a verified source boundary and show the resulting spoken units before applying it. Preserve canonical turn ownership and uncertain-evidence metadata.

Use ordinary labels such as **Display timing**, **Spoken passage** and **New utterance**. Give a specific recovery action when an edit needs a passage-level operation. A save error should identify the affected row and keep the draft intact.

### Draft protection and preview

Add undo for deletion and reversible local edit operations. Track dirty state. Closing/navigating prompts **Save / Discard / Cancel**; a failed save keeps the draft and shows the repair path. Preserve draft state across an in-app route switch for the same selected artifact/revision; a changed source requires explicit reconciliation. Durable browser-local crash recovery can follow if its privacy/storage limits are designed, but it is not required for the first release increment.

Add a managed compatible video preview derivative and an explicit audio-only fallback. Handle both codec errors and a loaded audio-only file with zero video dimensions. Keep preview generation bounded/cancellable and protect original media. Reuse the artifact preview lifecycle and caching patterns.

Gate: mixed display/spoken passage cases, Japanese/Chinese boundaries, speakers and overlaps, keyboard focus and dirty-close behavior, failed save/retry, deletion undo, source revision conflict, compatible MP4/WebM plus unsupported original codec, captions following unsaved edits, and effective 390-pixel layout.

## Phase 5 — Complete the multilingual project workflow

### Project board and readiness

Extend the existing Languages cards and typed project payload; retain their direct links. Show pinned correction/timeline version and time, with a notice if the source now differs. Existing branches stay on their pinned source until the user explicitly creates a new project/checkpoint path.

Per language, show translation, review, voice/plan, generation and export readiness, plus active job/failure reason and a direct next action. Distinguish “translation produced” from “reviewed.” Use current artifacts, speech-plan review records and jobs as authorities; avoid duplicating their state in an independent workflow engine. Group related language sessions in the session list without removing independent session navigation.

Show effective per-language provider/model/voice and subtitle limits with provenance: automatic language defaults, copied source settings, or custom override. Changing a shared plan must not silently rewrite existing branch settings/results.

### Selected-language actions

Provide separate **Translate selected**, **Generate selected** and **Export selected** actions. Preview eligible/skipped languages, exact pinned inputs/settings, required downloads and job count. Translation results remain reviewable. Generation requires reviewed, current speech-plan inputs and compatible voice configuration. Do not chain translation straight into synthesis with an implied approval.

Use durable idempotent operations that track child job IDs and per-language outcomes. Recheck revisions before enqueue. Reuse existing worker/resource limits; no unbounded GPU fan-out. On partial failure, retry only failed eligible children and preserve successful artifacts. Cancellation clearly reports which children already completed or are still stopping. Expose equivalent HTTP/MCP operations with the same guards.

Collect completed selected exports into a manifest and optional ZIP, using stable project/language/version filenames. Include artifact IDs/hashes and output kind. A subtitle-only export must not depend on successful audio/video generation. Never mark an incomplete bundle complete.

Surfaces: `translation_projects.py`, `translation_project_routes.py`, current jobs/idempotency/workflow/export services, generated response models and MCP session-branch tools; parent-owned Languages page, session grouping, progress/actions and output collection UI.

Gate: English source with Japanese/Polish/German branches; subtitles-only versus voiceover; single/multiple voices; stale source/settings; one blocked language; one failed child; repeated request/retry; app/worker restart; cancellation; failed-language-only retry; correct ZIP/manifest names/hashes and unchanged completed branch artifacts.

## Phase 6 — Release acceptance

- Rehearse migrations on a copy of an old supported workspace; preserve backup/rollback material and verify session/fork/source/purge references.
- Build from the final source commit, retain artifacts/manifests, and test independently installed app/MCP/Manager packages. Select fresh publication versions and record channel/version provenance.
- Run full required quality/test lanes and wait for Linux/Windows Chromium/Firefox CI. Check real rendered flows, keyboard/focus/accessibility, mobile layout and existing-session values; do not substitute source-string assertions for behavior.
- Verify pinned audio.cpp/ASR assets, native CLI contracts and representative model/locale routes. Use bounded real ASR/translation/TTS acceptance separately from stub routing tests; record models, costs and inputs. Do not claim equal quality for every advertised language.
- Resolve the live Manager identity mismatch through supported configuration before live acceptance. Preserve identity checks. Before any later authorized activation, check running jobs/Manager operations, retain the previous slot and database backup, promote the exact verified artifact, then verify API/worker/MCP identity and browser behavior.

Release exit: confirmed defects resolved, new capabilities exposed consistently, no failing required gate, package/install and migration evidence retained, and any untested platform/provider boundary stated in the release record. Publishing/activation is a later requested action.
