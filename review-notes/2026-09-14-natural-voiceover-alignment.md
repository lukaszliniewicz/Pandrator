# Natural-first cue-to-voiceover alignment

Date: 14 September 2026
Repository: `/home/lliniewicz/Projects/Pandrator_dev/Pandrator`
Baseline: `b933e71a` (Preserve logical passage boundaries when sizing speech blocks)
Status: implemented and tested in the working tree. Not committed, pushed, deployed, or loaded by restarting the application. Existing sessions, selected takes, and original audio were not changed.

## Findings

The current optional early-timing repair already splits text and regenerates the two children. It does not cut an existing waveform into cue-sized pieces. `pandrator/web/voiceover_repair.py::repair_early_blocks` stages an inactive revision, synthesizes replacement children, preserves unaffected takes, and only activates the new revision after validation. The waveform renderer is display-only; the output fitter processes whole speech files.

Correction and translation already preserve logical merges correctly. `pandrator/web/logical_passages.py::map_output_passages` validates evidence references and the combined source window. Merged text keeps the minimum source start and maximum source end; old internal cue boundaries are not reintroduced as precise timings. Display reflow remains separate from the logical passage ledger.

Actual defects were in automatic capacity splitting, placement slack, and the interaction between slowdown and repair eligibility. In particular, original passage edges could still be used as capacity cuts even where they were not natural speech boundaries.

## Implemented behavior

### Natural boundaries

`pandrator/logic/dubbing/natural_boundaries.py` provides shared punctuation and language-aware conjunction rules. The prior language dictionary remains available for compatibility, while actual splitting uses a narrower clause-opening dictionary. Bare coordinators such as `and` and `und` do not independently license a cut, since they may join names or noun phrases. Examples of permitted clause openers include `because`, `although`, `weil`, `obwohl`, `omdat`, and `ponieważ`.

Sentence endings outrank strong clause marks, commas, and unpunctuated clause-conjunction seams. The helper also guards decimal/thousands separators, clock times, abbreviations, initials, token-internal periods, closing quotation marks, and ellipses. These are deterministic heuristics, not a claim of full syntactic parsing or listening validation in every language.

`speech_blocks.py` no longer falls back to arbitrary whitespace or character cuts. Even a stored source-passage edge must be a natural seam before it can be used merely to satisfy a capacity limit. Whole passages remain preferred when a natural grouping fits the cap.

When the hard text cap makes whole-passage grouping impossible, natural internal TTS chunks may share the original passage timing envelope. Shared evidence references keep those chunks in one alignment group instead of fitting the source window independently multiple times. Such chunks carry `shared_passage_timing` and `timing_basis=shared_source_window`; character-proportional internal cue timestamp estimates have been removed from this capacity path.

Text that cannot fit the cap at any permissible natural boundary raises `UnsplittableSpeechBlockError` with the affected references and lengths. The remedies are an appropriate cap within the engine's capabilities, a natural editorial boundary, or a wording correction, not silently cutting a word or noun phrase.

### Placement, tempo, and catch-up

Gentle slowdown is on by default, down to 0.9x. Explicitly saved off settings remain off. This is consistent across built-in settings, assembly, and repair preview.

Start-delay slack now uses the speech block's own cue span. A long gap before the next utterance is no longer permission to delay a brief reply until after its own cue has ended. That following gap remains available for natural overruns and catching up.

A small total overrun is tolerated: the smaller of 150 milliseconds and 5% of the scheduling window. Carried drift consumes this allowance rather than receiving a new allowance at every block. Larger overruns use the existing bounded speed-up mechanism; unresolvable remainder is carried forward, not cut off. Blocks carrying delay are neither slowed down nor given an additional start delay.

Explicit zero sentence gap is preserved. The previous `value or 100` fallback could silently replace zero with 100 milliseconds.

### Regeneration safety

Early-repair eligibility now uses measured rendered duration after gentle tempo adjustment, rather than the raw take's duration. A block that is sufficiently aligned after slowdown should not trigger needless regeneration.

Repair still requires trustworthy complete text-to-cue provenance and natural boundaries in both display and spoken text. It skips already-estimated internal timing. After regenerating, it checks the new internal anchor as well as final downstream delay: a first child that substantially overruns the second child's anchor is rejected even if later catch-up would hide that error. Original selections remain intact on failure, cancellation, an unsuitable replacement, or a concurrent user selection change.

## Real-session review

Session: `a5f8440d-ef59-4f52-8e19-7cfd379923cc`, Pascal full logical-passages test, English to German.

The last completed repaired run inspected was `66dc1978-19fb-4735-8aef-1fee2f6fb52b`, with 554 selected takes. Its saved assembly settings explicitly disabled slowdown. Its assembly had zero final drift, which does not establish good local semantic alignment. A subsequent repair attempt was canceled.

A full planner replay used the recorded post-baseline logical-passage provenance in `tmp/pascal-full-logical-passages/speech-plan-capacity-fixed.json`, not a replacement of the active session.

| Check | Result |
|---|---|
| Source passages | 1,655 |
| Speech blocks before and after | 542 / 542 |
| Maximum block length | 297 characters against cap 300 |
| Full text and order | Preserved |
| Original source windows | Preserved |
| Duplicated text or repeated source references in this replay | None |
| Questionable capacity seams detected by the shared rules | 3 before / 0 after |

The questionable original seams included a cut after `Namen wie` and a cut between a sequence of names and `und andere`. The replay found natural alternatives without increasing block count or splitting any passage reference across blocks.

## Real-audio comparison

Two existing TTS take copies were processed using the baseline and changed fitters. This was a synthetic test sequence of real generated audio, not two adjacent passages from the original talk, and not new TTS synthesis.

A 400 ms reply had a 640 ms cue and 8,920 ms until the next start. The old artificial start delay was 1,000 ms; the changed delay was 168 ms, so the reply remains within its cue. A separate 9,680 ms German passage became 10,738 ms under gentle slowdown. Final drift in the comparison was zero. Original-file SHA-256 hashes remained unchanged.

Results and local WAV comparisons are in `tmp/natural-voiceover-review-20260914/`:

- `replay.py`, `replay-results.json`, and `natural-speech-plan.json`.
- `real-take-comparison-before.wav` and `real-take-comparison-after.wav`.
- `final-regression-tests.log`.

## Verification

Combined focused regression run: **374 passed**, with one existing Python `audioop` deprecation warning. Coverage includes natural boundaries, speech planning, logical correction/translation merges, both assembly backends, cumulative drift, rendered-duration repair eligibility, child-anchor validation, cancellation, concurrent selections, and repair history.

Command, from the repository root:

```sh
PATH="$PWD/.pixi/envs/default/bin:$PATH" .pixi/envs/default/bin/python -m pytest -q \
  tests/test_dubbing_audio_sync.py tests/test_voiceover_natural_timing.py \
  tests/test_dubbing_early_repair.py tests/test_speech_block_natural_splitting.py \
  tests/test_dubbing_speech_blocks_integration.py tests/test_speech_block_passage_capacity.py \
  tests/test_speech_block_prosody_regressions.py tests/test_dubbing_logical_passages.py \
  tests/test_web_logical_passages.py tests/test_dubbing_llm_correction.py \
  tests/test_dubbing_llm_translation.py tests/test_web_voiceover_repair.py \
  tests/test_web_workflow_handlers.py tests/test_web_generation_topology.py \
  tests/test_web_speech_planning.py tests/test_web_repair_history_deletion.py \
  tests/test_dubbing_settings.py --tb=short
```

`git diff --check` passed. The two new test modules were registered in `scripts/test_lanes.py`. The repository-wide lane-manifest check remains blocked by nine pre-existing unregistered test modules; this is not a claim that the full repository suite is clean.

## Limits and rollout

No full-session fresh TTS generation or subjective listening comparison was performed. The audio comparison validates real processing, durations, and preservation; synthetic generation tests validate regeneration control flow. They do not prove the prosody of every future TTS output.

A merged or reordered passage without trustworthy internal target-text anchors is deliberately kept within its shared timing window. The planner does not resurrect old source-word timestamps merely to make synchronization look exact. Genuine speaker overlaps and unusual source timestamps can still require editorial review.

New defaults do not overwrite the explicit slowdown-off setting in the latest Pascal session. To evaluate that session with slowdown after loading this code, enable it explicitly and create a separate comparison output. The original takes remain suitable as a baseline.
