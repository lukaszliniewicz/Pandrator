# Generation controls and voice-library annotations

Reviewed against `9d0e5f4752be8e8252bd63c529c81de8025943bf` on 12 September 2026.
This follows the completed #641 and #588–595 review recorded separately.

## Starting another run

The affected session had a passive subtitle-editing dispatch still marked running
from 8 September. Speech-plan readiness used the same lock as replacing source
media, disabling audio generation even though synthesis consumes a frozen plan.
Generation readiness now ignores passive editing dispatches. Active jobs/audio
runs still block it; source replacement and plan preparation/selection retain
their original locks. No live dispatch was canceled or rewritten.

The generation card now says **Start new run from plan**. The drawer offers
**New run** alongside **Resume previous run** for paused runs. A new run uses
current voice/settings; resumption keeps the previous run's saved settings.

A disposable browser fixture reproduced an open editing dispatch and paused run.
Starting from the card created run 2 with the newly selected voice and the exact
same plan ID. Run 1 remained paused; the dispatch remained running; both blocks
completed and became reusable. Speech-block settings saved without rebuilding
the plan or dropping the voice configuration.

## Annotation coverage

| Annotation | Change |
| --- | --- |
| 1 | Muted review and stale-only generation actions have visible borders. |
| 2 | Removed the redundant Resolved outcome heading/description. |
| 3 | Speech-plan selectors use padded, bordered controls with focus/disabled states. |
| 4 | Stage/design dialogs clip an inner styled scroll area inside their rounded shell. Stage and plan settings sit above the generation drawer. |
| 5 | Available library references are collapsible, full-width rows; long names wrap. |
| 6 | audio.cpp no longer advertises true batching/streaming; its batch control is hidden. |
| 7 | Five deterministic speech-block settings moved to a dialog on the plan card. Saved plan versions remain unchanged until a new plan is prepared. |
| 8 | The new-voice input submits on Enter, with a duplicate-submission guard. |
| 9 | Transcription options are collapsed by default. Automatic selects Parakeet for its supported languages and Whisper otherwise. API callers get the same voice-reference defaults; explicit choices remain respected. |
| 10 | Voice language is directly editable beside the sample actions. Transcript review now uses that selected language instead of the new-voice form's default. |
| 11 | Auditions use fresh random seeds by default. Optional fixed-seed mode supports reproducible comparisons. |
| 12 | A longer common passage is translated into all 10 supported Qwen design languages; Breeze uses its English/Chinese subset. |
| 13 | One action generates 1–4 candidates, serially to avoid competing for the local engine. Each is playable and selectable; only the selected artifact is promoted. |

## audio.cpp transport review

`AudioCppAdapter` previously advertised `pandrator-ordered-serial-v1` batching,
but iterated ordinary single-segment synthesis. Each audio.cpp HTTP request has
one `input`, without `items` or streaming. Pandrator already pools HTTP sessions
and serializes requests per normalized endpoint. The inline reference is encoded
with a client cache, but its base64 content is repeated in each request.

The installed audio.cpp v0.7.2 serves the configured models in offline mode.
The nearby source checkout is a different revision (`30d4d42`), so its optional
single-request streaming implementation is not evidence that the installed
service supports multi-input batches. Kobold's real NDJSON batch transport remains
enabled and unchanged. Legacy audio.cpp serial batch-compatible calls still work;
the public capability flags now describe the actual transport.

No GPU throughput benchmark or transport-vs-inference timing claim is made.
Existing logs did not supply those measurements. Further optimization of reference
uploads or engine scheduling needs measurements against the installed server;
increasing the old batch-size field cannot provide that improvement. This change
removes the misleading tuning control while retaining pooling and ordering.

## Validation

- Source-plan/backend/audio-identity group: **42 passed**.
- audio.cpp/provider profile group: **21 passed**; TTS handler group: **72 passed**.
- Voice defaults/library group: **33 passed**; language policy: **8 passed**.
- Final optional-body/defaults regression file after review: **7 passed**.
- Ruff on all changed Python files and `git diff --check`: passed.
- Differential Pyright on the five changed backend modules: **49 existing
  diagnostics on both sides, no new diagnostics** (line shifts ignored).
- Vulture at 100% confidence on `voice_library.py`: no findings. Static import
  inspection and fresh-process imports show no cycle from the added language helper.
- Svelte check: **0 errors, 0 warnings**; scoped ESLint and production build passed.
- Parent browser verification: paused-run reproduction; new run on unchanged plan;
  settings and library disclosures; Enter submission; German→Parakeet and
  Japanese→Whisper; all 10 localized passages; two three-candidate auditions with
  distinct fresh seeds; candidate 2 playback/promotion; two fixed auditions
  preserving seeds 42/43; modal corners and layering.
- Saved sample metadata matched selected candidate 2's artifact, seed, exact
  German passage and reviewed transcript. Provider linking completed locally.

Browser checks used a disposable database with synthetic 600 ms audio replacing
only the provider synthesis call. Queueing, API requests, artifact storage,
normalization and promotion used the real application paths. No live session run
was started, and real model synthesis/recognition quality was not assessed.

An independent backend review found an empty-JSON-body compatibility regression;
it was fixed and covered before release. No other material backend finding remained.

Specialists used: Terra/high deep-researcher for audio.cpp transport evidence;
Luna/xhigh researcher for voice API/language facts; Luna/xhigh implementers for
truthful capabilities/tests and voice-reference transcription defaults; Terra/xhigh
reviewer for the final non-UI review and differential quality checks. Parent owned
all UI, product decisions, integration and browser verification. No external model
or OpenCode route was used. The language researcher checked the primary
[NVIDIA Parakeet v3 model card](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3).
