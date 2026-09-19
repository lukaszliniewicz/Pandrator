# Instructions, emotions, and model conversion review

Reviewed commit `b8b53df9e1c6e2e41ecfff85dbc18b9ab5071114` against its parent, plus the current integration. Review only; application source was not changed. Existing untracked files were preserved.

The foundation is sensible: annotations stay separate from spoken text, conversion is model/backend-aware, unsupported controls are represented explicitly, and generation takes an immutable snapshot. The main weakness is the boundary between what the user reviews and what generation actually consumes. The current interface also exposes much less of the annotation than the data model supports.

## Confirmed findings

### 1. P2 — Vertex request limits are not enforced on the compiled request

`pandrator/logic/speech_performance.py:623-643` checks a universal 16,000-character context maximum, then adds instructions, labels, and transcript. `pandrator/logic/tts_handler.py:6440-6461` sends that result directly as Vertex `contents`.

Google documents an **8,000-byte contents limit for the Vertex AI API**. This is distinct from the separate 4,000-byte fields on the Cloud Text-to-Speech API. [Official Gemini-TTS documentation](https://docs.cloud.google.com/text-to-speech/docs/gemini-tts#use_vertex_ai_api).

A local compilation with `before = '前' * 16000` and a short Japanese transcript succeeds and produces **48,337 UTF-8 bytes**. Even the default 4,000-character window can exceed the byte budget with CJK text. The code therefore accepts requests outside the documented provider contract; no paid/live rejection test was made.

Enforce the actual route's budget after accounting for the complete envelope, directions, and transcript. Preserve the transcript and expose any context reduction or an actionable validation error. Do not silently truncate spoken content.

### 2. P2 — Compiled preview and generation use different context limits

`pandrator/web/performance_plans.py:688-700` builds preview context from the limits saved in the performance plan. `pandrator/web/speech_plan_workspace.py:246-257` freezes generation context from current runtime TTS settings instead.

Reproduction by source trace: create a plan with `context_before=0` or `context_max_chars=0`; subsequently save nonzero synthesis limits and enable Gemini context. Preview still omits neighbours, while generation includes them. The inverse also occurs. The UI does not submit its before/after/character-limit controls to preview (`web/src/lib/PerformancePanel.svelte:301-306`).

Planning context and synthesis context may legitimately differ. The defect is that the **compiled request preview** does not use the same synthesis configuration as a new run. Preview should resolve that configuration through the same path as generation and identify whether it represents saved or unsaved settings.

### 3. P2 — The normal editor hides active emotions and other controls

`web/src/lib/PerformancePanel.svelte:124-135` loads only `delivery.instruction` into the basic editor. On save, `:254-265` preserves the original emotion, pace, cadence, emphasis, spans, and events. Yet the editor promises “Leave blank for normal delivery” at `:590`.

Browser reproduction: an analysed annotation with `delivery: { emotion: 'sad' }` displays a blank direction; saving it preserves an active sad emotion and `decision='steer'`. Clearing visible instruction text likewise does not clear hidden controls. After adoption, the disabled editing fieldset also prevents opening the JSON checkbox to inspect the full annotation.

Show a readable summary of every active control, including phrase ranges and events, on drafts and adopted plans. Provide an explicit “Use normal delivery / clear directions” operation. JSON can remain an advanced editor, but should not be necessary to understand what was adopted.

### 4. P2 — Basic and advanced editors can silently discard each other's edits

`web/src/lib/PerformancePanel.svelte:131-135` initializes JSON from the saved annotation. `:254-255` chooses either JSON or the basic editor as authoritative, without synchronizing them. The toggle at `:602` changes authority immediately.

Browser reproduction: type a new unsaved direction, then enable advanced JSON. The JSON still contains the old saved instruction; saving at that point submits the old value. Toggling back similarly ignores unsaved JSON changes. The explanatory text that JSON is authoritative does not preserve the user's edit.

Use one canonical draft annotation and convert the presentation when switching editors. Invalid JSON should leave the user in the JSON editor with a clear error, rather than discard it.

### 5. P2 — Routine reloads discard edits and reset the reviewed block

`web/src/lib/PerformancePanel.svelte:156-175` unconditionally replaces all settings and selects the first block of the page. Saving an annotation calls that same loader at `:287`; Refresh, page navigation, and version changes also use it.

Browser reproductions:

- Saving block 2 jumps selection to block 1. On a long page, the next edit can easily target the wrong block.
- Refresh discards an unsaved general narrator direction without warning. It also reloads the annotation editor; the documented workflow asks users to Refresh to see analysis progress.

Preserve the selected block when it still exists. Track dirty settings and annotation edits separately; background/progress refresh should not overwrite them. A deliberate navigation that would discard a draft needs a clear save/discard choice.

### 6. P2 — A displayed compiled preview can become stale without any indication

`web/src/lib/PerformancePanel.svelte:296-306` stores a preview response, but changing direction, JSON, global guidance, context mode, or the vocalization toggle does not invalidate it. Preview is cleared only when a block is chosen (`:136`).

Browser reproduction: compile a preview, change the delivery direction, and the previous compiled result remains visible without a stale marker. This undermines the principal review affordance even before considering finding 2.

Invalidate the preview on any compilation-affecting input change, or label it clearly as belonging to the previous draft. Adoption should make it clear which saved inputs have actually been reviewed.

### 7. P2 — Context truncation can remove the other-speaker label

`pandrator/web/speech_plan_workspace.py:185-190` prepends `[Other speaker: …]`, then `:211-216` truncates preceding context by retaining the end of the whole formatted string. A bounded window can therefore retain another speaker's words while dropping the label that distinguishes them from the current speaker.

A focused local reproduction with a small character budget produced a suffix beginning `or] Actual question?`. The same issue occurs with long neighbouring blocks and larger budgets.

Budget the context text while preserving its speaker attribution as a complete unit. This matters for interpretation, although it does not directly change the selected voice.

## UX and workflow assessment

I recommend **Speech direction review** as the review-step label, **Speech direction** for the feature, and **Analyse delivery** for the optional LLM action. “Performance” is ambiguous here; “model instruction review” can sound like reviewing the LLM's own prompt. Keep pSSML as an advanced format name.

Keep the workflow visibly ordered: accepted speech text → speech blocks → optional speech direction review → generation. The direction step needs a compact status even while collapsed: disabled, draft, adopted, stale, or incompatible/partly supported on the selected model. The existing panel can provide this without introducing a new pipeline framework.

The current panel mixes session-wide voice/direction settings, synthesis-context settings, planner configuration, version history, block editing, and provider diagnostics. Desktop and 390px mobile inspection found a readable but very long form. A clearer split would retain global voice/direction settings with generation, expose analysis settings when starting analysis, and devote the review surface to the annotations and their model compatibility.

For book-length review, a 20-item dropdown/page is insufficiently informative. A compact block list with filters for directed, unreviewed, unsupported, and locked items would make the optional analysis useful at scale. Show neighbouring context alongside the selected block, rationale, and annotation summary. Version choices need distinct identifiers or timestamps; status and directed-count alone produce indistinguishable entries.

The analysis job currently exposes an ID and requires Refresh. Show its state and progress, provide a job link, and preserve local edits during refresh. Also display the selected plan's saved planning guidance/model/context configuration; the inputs currently remain creation-form values rather than reflecting the selected plan.

## Conversion and adaptability assessment

The separation of canonical annotations from provider syntax is worth retaining. Model variant plus backend route is a better capability boundary than provider name. Unknown routes remain conservative, and compilation distinguishes applied, approximated, unsupported, and disabled controls.

The provider research supports the broad mapping:

- Fish S2 documents free-form inline bracketed directions and vocal events. It does not promise exact tag scope or a universal reset command. [Fish documentation](https://docs.fish.audio/developer-guide/models-pricing/models-overview).
- Qwen's executable wrapper and README support instruction control for 1.7B CustomVoice/VoiceDesign and omit it for Base/0.6B. Its 0.6B model card conflicts with those sources; conservative behavior is appropriate until the deployed backend is verified. [Qwen README](https://github.com/QwenLM/Qwen3-TTS/blob/main/README.md), [official wrapper](https://github.com/QwenLM/Qwen3-TTS/blob/main/qwen_tts/inference/qwen3_tts_model.py).
- Gemini supports style prompts and inline expressive controls. Context exclusion and phrase scope remain model-interpreted. The shared profile includes spellings such as plain `[pause]` and `[chuckles]` that are not established by the specific Cloud tag table checked; this is a documentation/quality uncertainty, not proof of rejection. [Gemini speech guide](https://ai.google.dev/gemini-api/docs/generate-content/speech-generation).

Near-term adaptability work should concentrate on route-specific request budgets and a visible compilation/compatibility summary when the TTS model changes. Do not force different models into a shared promise of exact emotion, phrase scope, or timing. Preserve unsupported canonical intent so changing to a capable model can recover it.

There is also a settings distinction that the workflow needs to expose: a plan's `allow_vocalizations` authorizes annotation analysis, whereas generation separately requires runtime `performance_allow_vocalizations`. Adoption only sets `performance_enabled` (`performance_routes.py:231-244`); the compiler drops non-pause events when the runtime flag is false (`speech_performance.py:567-572`). These can be legitimate separate controls, but an enabled preview based on unsaved settings followed by adoption can mislead the user. Show effective saved generation behavior before adoption; do not silently enable vocalizations merely because an LLM proposed them.

## Verification and limits

- Existing contextual-performance Chromium test: passed using installed Chrome.
- Temporary browser probe: passed five observations covering hidden emotion, basic/JSON divergence, stale preview, selection reset, and dirty-settings loss. It used mocked performance endpoints to isolate real UI behavior, not prove backend behavior. Temporary test source was removed from the repository and retained at `/tmp/pandrator-performance-review-probe.spec.ts`.
- Desktop and 390px mobile screenshots inspected.
- `npm run check`: zero Svelte/TypeScript errors and warnings.
- ESLint on `PerformancePanel.svelte`, `SpeechPlanCard.svelte`, and `GenerationSegmentTable.svelte`: passed. The wider `SessionWorkspace.svelte` check found three pre-existing errors outside this feature's one-line integration change.
- `.venv/bin/python -B -m pytest -p no:cacheprovider tests/test_performance_plans.py tests/test_mcp_performance.py`: 22 passed, 1 skipped because the optional MCP SDK is absent.
- `tests/test_speech_performance.py`: 30 passed.
- Focused backend Ruff: two pre-existing unused imports in `workflow_handlers.py`; confirmed against the parent commit. No newly introduced Ruff findings in the checked scope.
- No live synthesis, paid provider request, listening test, complete repository audit, or exhaustive provider/version matrix was performed. Compilation tests establish request construction, not acoustic compliance.

Specialists used: `reviewer` on Terra/xhigh for backend evidence and focused validation; `researcher` on Luna/xhigh for official provider contracts. The parent performed all UI inspection, browser probes, integration assessment, and final prioritization. No OpenCode or other external-model route was used.
