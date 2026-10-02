# Release implementation and acceptance record

The accepted scope is the complete [release plan](2026-10-02-release-plan.md). The user authorized implementation, commit, and updating the local installation before creating a release. The original audit records the audited commit; this document tracks the resulting implementation.

Release versions: **Pandrator 0.11.0, MCP 0.6.0, Manager 0.9.28**. This is the qualification snapshot after first candidate commit `4a5bac09` and its platform-CI repair, before local promotion and publication. The final release acceptance report records the resulting source identity, installation and public artifacts.

## Implementation and acceptance

| Requirement | State | Evidence / remaining gate |
| --- | --- | --- |
| P1. Settings payloads and legacy migration | Implemented; accepted locally | Revision-safe PATCH, alias precedence, focused regressions; browser save/reopen passed |
| P1. Independent MCP package | Implemented; package preflight accepted | Independent MCP 0.6.0 wheel registered 162 tools without Pandrator/Manager/SQLAlchemy |
| P1. Canonical reviews and strict single voice | Implemented; focused acceptance passed | Role/ID/ancestry, speaker persistence, strict-single preview/runtime tests; browser speaker save/reopen passed |
| P1. Upload/purge lifecycle | Implemented; local acceptance passed | Active writer, retry, source ownership and path guards; integrated lanes passed |
| P1. Release gates | Implemented; final acceptance running | Collection and lane registration repaired; stale assertions corrected around explicit voice/default behavior |
| P2. Exact language capabilities | Implemented; mechanical acceptance passed | Provider/model/revision/operation/native route records; exact/subset/claim/unknown distinguished; 745 registry languages; 132 aliases; Silero/cloud/native coverage separated |
| P2. Operation-aware pickers | Implemented; browser accepted | Searchable registry and saved custom values, exact TTS model support, separate ASR/alignment coverage; Swahili/Burmese/Khmer/Lao/pt-BR/nb searches passed |
| P3. Automatic ASR | Implemented; native acceptance recorded | Parakeet preferred for covered languages, Qwen when recognition/timing permit, Whisper fallback; bounded CPU Tiny detector; explicit overrides preserved |
| P3. Untimed TXT | Implemented; focused acceptance passed | Recognizer-only Qwen route produces no invented cue timestamps; timed workflows reject unavailable alignment |
| P3. Demucs and 0.9 catalogue | Implemented; native acceptance recorded | Four-stem Q8 option off by default; six-stem catalogue; verified CPU/GPU/cancel/source checks and bounded benchmark |
| P4. Passage/display editing and drafts | Implemented; browser accepted | Saved review starts clean; failed saves retain drafts; Delete/Undo, Save/Discard/Cancel, speaker persistence and 390 CSS width passed; Japanese anchored split, Chinese combined-cue ownership and composition-conflict guard passed; bounded cache isolation/stale fences checked |
| P4. Compatible media preview | Implemented; native/browser acceptance passed | Unsupported FFV1 original loaded with zero video dimensions; compatible MP4 and audio fallback playable; controlled native cancellation left original intact and no registered derivative; canceled copy clarified |
| P5. Readiness/provenance/session grouping | Implemented; browser accepted | Pinned checkpoint/hash/time, exact output-settings readiness, independent translation/review/voice/gen/export; source-first grouped session listing and blocked-generation explanation passed |
| P5. Selected-language operations | Implemented; focused acceptance passed | Durable guarded previews, receipts, retry/cancel/passive authority and tombstones; real JA/PL/DE subtitle exports completed in browser |
| P5. Manifest and ZIP | Implemented; browser accepted | Verified names/hashes and complete-only ZIP; deep retained-retry traversal and committed-publication crash recovery; forged receipt rejection; downloaded three-language ZIP verified against manifest and managed files; restart/repeat recovered the same completed bundle |
| P6. Quality and platform CI | Local acceptance passed; platform CI pending | 4233 non-UI tests passed, 10 skipped, plus 36 parent-owned UI/source tests; frontend quality/build and locked Ruff/types/cycles/dead code/lane checks passed; typing baseline strictly reduced 1209→1199 (10 removals, zero additions) |
| P6. Migration | Passed | Actual old-HEAD 0051 fixture cloned and migrated to 0052; 73 existing tables/six managed files unchanged; integrity/FK/reference checks passed |
| P6. Final installed packages / real TTS and translation | Six-archive preflight passed; TTS passed; external translation blocked | Strict metadata/content audits passed after excluding internal plans from the app sdist; independent MCP and Manager core imports passed; actual cached Kokoro synthesis passed. Go requires a session header and Vertex billing is disabled; no successful live translation is claimed |
| Commit / local update / release | First candidate committed and staged; final qualification pending | Preserve staged candidate; commit CI repairs and rebuild from that source, then idle check/backups/retained previous slot/API/worker/MCP/browser parity and publication |

## Decisions and evidence boundaries

- Automatic ASR is a new/inherited default. Existing explicit selections remain overrides. Recognition support and timestamp/alignment support are separate capabilities.
- Unknown model language coverage remains unverified; it never means every language. Speech preflight checks the effective per-segment voice/model, including strict-single behavior, before synthesis. Legacy workflow requests check supplied plan IDs, current session/revision and known source ownership before enqueue and at the worker boundary.
- Project exports remain independent of speech generation. A completed ZIP is an immutable copy; an active bundle job still blocks deleting its input. A verified committed pair can be recovered after a worker fails before recording completion, without duplicated outputs. Purging a branch scrubs its private operation captures and leaves a tombstone.
- Browser fixtures are synthetic and isolated under `/tmp/pandrator-browser-acceptance-20261002`; they are not evidence of LLM translation. The application/worker were restarted with retained fixture state. A version change correctly invalidated an old target-bound export operation; a fresh current-version preview was required.
- The preview cancellation test used the real installed FFmpeg through a disposable input-throttling shim so the cancel control could be exercised reliably. Native process stopped, source hash stayed unchanged, and no derivative was registered.
- See [native ASR and separation evidence](../review-notes/2026-10-02-pre-release-evidence/native-asr-and-separation.md). Japanese Qwen CPU/Vulkan output has point-like raw word timestamps, while all four emitted display cues have valid positive spans. This is a recorded native limitation, not a claim of all-word timing precision.
- Demucs has a smaller download and measured GPU performance on the retained synthetic inputs. Whole-process timings do not separate cold load, long CPU tests hit bounded time/cancel limits, and no listening-based quality comparison was performed. No general acoustic-quality or every-backend speed claim is made.
- The current Manager identity mismatch was reproduced during fresh native checks. Manager restart adoption assigned surviving services to its new instance, while their process environments retained the old instance ID. Controlled core-service restart during the backed-up local promotion will refresh those IDs; identity checks remain enabled. Unsigned development-slot doctor pointer errors are a separate provenance issue.
- Specialists used custom GPT-6.1 Sol/high and GPT-6 Luna/max. These are configured assignments; independent runtime model identity was unavailable. Parent owns product decisions, UI, integration and acceptance. Muse/OpenCode external research was not used for this audit.

The first platform run exposed a Windows CP1252 import failure in the expanded catalogue and a Node 24 JSON import-attribute failure before browser-test collection. Both were repaired explicitly. The catalogue/model focused suite passed 29 tests, including two legacy-Windows-encoding regressions; frontend quality/build passed and browser-test collection found 514 tests. Full platform reruns remain required for the repaired commit. The Python typing baseline was not expanded.

One Ubuntu native-progress assertion also needed to account for FFmpeg emitting `N/A` before a timestamped frame. Its numeric/monotonic/final-record checks remain enforced; all ten focused media-process tests passed. This was a test-only repair.

Temporary evidence roots: `/tmp/pandrator-final-acceptance-20261002`, `/tmp/pandrator-native-acceptance-20261002`, `/tmp/pandrator-native-tts-translation-20261002`. Durable acceptance summaries will be updated before publication.
