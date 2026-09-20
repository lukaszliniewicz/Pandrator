# Christmas Carol multivoice trial: findings and next phase

Investigated 2026-09-20 against checkout and deployed application commit
`e23e19be748bce083d1e19859a61b00fed68f896`. This is an evidence-backed proposal,
not an implementation or release record. No application code, session content,
model installation, or running service was changed. The live TTS catalogue was
refreshed, and read-only compilation previews were requested.

## Trial evidence

Session: `c75df9fa-b2ed-4b6a-b8ce-f2804741db16`, “A Christmas Carol - multivoice
feature test”. Its active generation plan contains 24 segments: 16 completed
and eight ready. The exported sample is 80.16 seconds; the 16 completed takes
together contain 115.52 seconds. Those are different quantities.

The source and cleaned text contain intact quotations and paragraph boundaries.
The saved prepared narration already contains the reported corruption. The
later speech-optimization artifact preserves that text and adds speech XML.
The active generation plan still points to the earlier prepared-text artifact;
its segment speech-plan fields are empty. The trial supplies its casting XML
through the adopted performance plan instead. This demonstrates working
rendering, but also a source-selection/discoverability problem: producing an
annotated artifact does not mean the active generation plan uses it.

Live previews and completed take manifests confirm the alternating character
and narrator voices. They also confirm that punctuation-only spans are sent to
TTS: six of the 38 completed render parts contain only punctuation, producing
1.84 seconds of audio. For example, `"Bah!" said Scrooge, "Humbug!".` becomes
four requests, the fourth being just a narrator full stop.

## Segmentation: reproduced, with attribution

The deployed splitter is **wtpsplit-lite 0.2.0**, SaT `sat-12l-sm`, running on
CPU ONNX. `ptsplit` is not installed. See the reproducible, no-GPU experiment
in `experiments/2026-09-20-multivoice-segmentation/`.

| Checkpoint | Observation |
| --- | --- |
| Raw excerpt through wtpsplit | 44 sentence pieces; no invented periods or orphan Humbug quote |
| Pandrator terminal-punctuation pass, then wtpsplit | 46 pieces; false periods and detached closing quote appear |
| Full current preprocessing | 24 rows reproducing the saved trial corruption |

The causes are distinct:

1. `text_preprocessor.py:136-149` only checks the last character when adding
   terminal punctuation. A closing quote is mistaken for missing punctuation.
   This runs before sentence segmentation at line 486.
2. NeMo changes `in ’em` to `in’em`; subsequent quote normalization produces
   `in'em`. This is a text-normalization issue, not a sentence-splitter issue.
3. Short-sentence appending uses `join_fragments`; its separator rule suppresses
   whitespace before a quote (`dubbing/text_units.py:51-67`). It cannot distinguish
   an opening quote from a closing one at this seam, producing `nephew."You`.
4. `sentence_continues_after` records length-enforced splits, not an ongoing
   speech. Every final row in this fixture has it set to false.

Raw wtpsplit still makes some boundaries inside quoted dialogue. That alone is
not necessarily a defect: a long speech needs bounded TTS requests. The missing
contract is preservation of quotation attachment, source paragraph, continuing
speaker and appropriate pause when those request boundaries are introduced.

Do not replace the statistical splitter on the evidence available. Fix the
proven preprocessing defects first, then evaluate quote-aware grouping around
its boundaries. Apostrophes, nested quotes and multi-paragraph speeches need
explicit representative cases; balanced quote counts alone are not sufficient.

## Continuation and repair controls

Audiobook split/merge is already exposed through
`pandrator_revise_speech_block_plan` and its atomic batch counterpart. The native
topology implementation is workflow-independent; the existing main topology
test fixture uses voiceover sessions. These are revision-fenced, idempotent
operations with restore support, not missing primitives.

However, they are not yet a complete multivoice repair workflow:

- Split and merge replace speech-plan markup with manual-topology metadata
  (`workspace.py:3630-3660`, `:3740-3785`). Character identities and cast remain
  session data, but detailed speaker spans are not projected into the new blocks.
- New plan revisions make previous performance plans stale. This protects
  against using wrong offsets but can force extensive re-review.
- Replacements do not copy generated takes; unchanged rows can retain take
  lineage. Reuse must also match the effective compiled request and voice.
- A simple merge inserts a space. It cannot by itself undo the false punctuation
  introduced in the current fixture; topology edits and text corrections must
  remain distinguishable.
- The adopted performance XML's `boundary_after="continuation"` does not update
  stored assembly pauses. Ordinal 10 still has 700 ms and ordinals 12/13/20 have
  250 ms. Native audiobook assembly reads `segment.silence_after_ms`
  (`workflow_handlers.py:10203`). Adoption updates the performance plan, not
  those segment fields (`performance_plans.py:816-877`).

The accepted speech plan should own structural boundaries and pauses. Delivery
annotations should reference that structure. A continuation label must either
change the effective assembly boundary through a reviewed plan revision or
explicitly report that it is semantic-only; it must not imply seamless audio.

## Model installation and selection

Observed versions: Manager 0.9.24, application 0.9.4, audio.cpp 0.8.1, Vulkan;
RX480 with 8 GB VRAM. The server is configured for one loaded model at a time.

| State | Models |
| --- | --- |
| Installed and returned by refreshed MCP catalogue | Qwen Base, Qwen VoiceDesign, VoxCPM2, FireRedTTS3 Base, Breeze TTS 2 |
| Currently loaded | Qwen Base |
| Catalogued but not installed | Qwen CustomVoice, Fish S2 Pro, plus other supported packages |

The report's “only Base is exposed” is not true of the refreshed live inventory.
The default application catalogue seeds Base; static supported-model metadata
is separate from selectable live inventory (`tts_handler.py:828-960`). The exact
cause of the earlier observation cannot be reconstructed from the report alone.

The MCP installation gap is real and precise. Manager audio.cpp resolution
requires a nonempty `options.models` list (`components/builtin.py:522-550`), but
MCP's `ManagerOptionValue` permits only scalars (`schemas/manager.py:44-45`).
Existing Manager machinery already stages, verifies, activates and validates
model packages, with add/remove/retain and byte estimates. Extend that contract;
do not introduce a second downloader or arbitrary shell/URL tool.

Qwen CustomVoice uses built-in speakers. It cannot retain the trial's cloned
reference cast unchanged. Fish's catalogue profile supports cloning; actual
quality, resource use and expressive behavior on this machine remain untested.
Breeze is already installed and its current compiler supports instructions and
optional cloning. It is a useful bounded experiment before downloading another
large model, not an acoustically validated replacement.

Compilation preview is not a readiness check: a CustomVoice preview currently
accepts this trial's cloned voice IDs and reports the instruction as applied,
even though CustomVoice is uninstalled and its mode is prebuilt. “Applied” here
means compiled into the request. Readiness, cast compatibility and audible
compliance must be separately reported. Previewing with an explicit service
override can also reset casting; comparisons need explicit effective settings.

## Proposed next executable phase

### Solve now

1. **Repair deterministic preparation.** Preserve punctuation through quotes,
   preserve spaces at dialogue joins, and protect leading-apostrophe words from
   harmful normalization. Keep paragraph and heading structure separate from
   TTS length constraints. Add this real excerpt as regression evidence.
2. **Make audiobook repair preserve multivoice structure.** Extend existing
   split/merge operations to project exact speaker spans and retain stable
   character IDs; provide bounded range resegmentation as a draft revision with
   source-offset lineage and an explicit before/after preview. Activation must
   show affected annotations/takes and retain restore history. Preserve only
   demonstrably unchanged annotations, leaving affected ones for review.
3. **Use one authoritative boundary for assembly.** Structural changes revise
   pause data; rendering folds punctuation-only fragments into an adjacent
   speakable request without discarding accepted punctuation or duplicating
   vocal events. Do not synthesize a standalone full stop as another character.
4. **Expose typed model selection through MCP.** Allow bounded validated model-ID
   lists through the existing Manager plan/execute lifecycle. An add-model
   operation preserves the other installed models unless removal is explicit.
   Report download/disk estimates, readiness, intended restart and cast impacts.
   Distinguish package activation, loading into memory and session selection.
   Refresh live capabilities after activation and prevent changes from disrupting
   an active generation job. Use existing permissions and plan confirmations.
5. **Improve preflight and guidance.** Surface the active source artifact and
   generation revision, model availability, incompatible cast bindings, unsupported
   directions and actual pause behavior before generation. Compilation success
   must not certify runtime readiness. Document the existing repair tools clearly.

### Test now

- Run the exact excerpt before/after and a small quote fixture set: ordinary and
  curly quotes, narrator interruptions, apostrophes, a long speech, and a speech
  spanning paragraphs. Require retained words/punctuation, no orphan closing
  quotes from preprocessing, no lost dialogue spaces, and correct boundaries.
- Use disposable audiobook sessions to exercise split/merge/range resegmentation,
  stale revisions, retry replay, restore, cast-span preservation, annotation
  invalidation and exact take reuse. Keep voiceover coverage intact.
- Verify model planning against existing installed models, invalid IDs, download
  failure, activation failure and a busy runtime. Validate tools and capabilities
  from a fresh MCP process after any rollout.
- After these pass, use a short Scrooge/Fred exchange to test one compatible
  expressive route. Keep text, cast and generation settings recorded; review
  audible identity, delivery, interruptions, pauses, latency and memory separately
  from compiler correctness. Expand to Stave I only after the excerpt passes.

### Defer

Replacing wtpsplit, wholesale narration-pipeline redesign, whole-novel generation,
an exhaustive engine benchmark, and promises of exact cross-engine voice identity
or emotional compliance. None is justified by this trial yet.

## Verification and scope

The CPU reproduction completed in 10.6 seconds using the actual deployed Python
3.11.15 environment. The new script passed Ruff. Live MCP inspection, compilation
previews and read-only saved-artifact/take inspection supported the findings.
No new speech was generated and no listening-quality judgment is claimed.

Specialists: `deep-researcher` on Terra/high for preprocessing reproduction and
non-UI revision/repair traces; `researcher` on Luna/xhigh for Manager/model/MCP
contracts and live model inventory. The parent audited the decisive source anchors,
inspected the trial, and owns this proposal. No external-model route was used.
