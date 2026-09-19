# Contextual performance planning and pSSML v1

Pandrator can direct a deliberately short speech block using the meaning of its
surrounding text. It does not have to synthesize a long passage and transcribe or
cut the result. No previous generated recording is used as an implicit voice or
prosody reference.

## Workflow and interface

In the review workflow, select the intended **Speech plan**, then expand
**Context and performance · pSSML**. This is available for narration/audiobooks
as well as timed voiceovers.

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
request without making an audio request. Advanced JSON supports phrase controls
and vocal events. A manual lock protects an annotation from reanalysis; changing
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
Japanese and other scripts without relying on spaces or word indices. Later
pronunciation/text/topology changes make an adopted sidecar stale rather than
silently remapping its anchors.

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
| Fish S2 through audio.cpp or native Fish S2 | Inline free-form tags | Inline phrase direction; explicit approximate restoration |
| Qwen 1.7B CustomVoice through audio.cpp | Separate instruction | Phrase intent described in the whole-request instruction |
| Qwen 1.7B VoiceDesign through audio.cpp | Separate instruction | Stable voice description remains required |
| Kobold Qwen's instruction-capable prebuilt path | Separate instruction | Not applied to its cloning path |
| Gemini / Vertex Gemini TTS | Prompt envelope | Phrase tags and bounded preceding/following semantic context |
| Supported OpenAI mini-TTS models | Separate instruction | Phrase intent approximated in the whole-request instruction |
| Breeze's audio.cpp instruction path | Separate instruction | Whole-request direction |
| Native Chatterbox Turbo | Listed vocal-event tags | No claim of general instruction following |

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
request options. Undirected requests retain their existing chunk settings.

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
History remains inspectable when a performance plan becomes stale; generation
requires a new valid adoption or disabling performance.

Automatic post-generation split/regroup repair is disabled while performance or
semantic context is enabled. It cannot silently change the accepted blocks and
reuse now-invalid phrase directions. Explicit repair is still available; review
its resulting plan and create/adopt matching performance before a new run.

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
