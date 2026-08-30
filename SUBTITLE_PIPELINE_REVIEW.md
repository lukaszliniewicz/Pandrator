# Subtitle and speech-block pipeline review

This file records implementation decisions and reproducible evidence for the
August 2026 subtitle/speech-block hardening pass. It intentionally excludes
credentials and private transcript text.

## Implemented architecture

- Subtitle display text and reviewed TTS text now use one paired partitioner.
- Every split chunk carries only the subtitle cues that actually contributed
  text to it; full-cue provenance is no longer copied to every child.
- Adjacent chunks that share one subtitle timing window receive an explicit
  `alignment_group`. Both assembly paths concatenate the audio in that group
  before fitting it to the subtitle window. Legacy shared-cue inference remains
  only for plans created before the new field existed.
- Generation plans and segment revisions persist alignment groups through the
  new `0026_generation_alignment_groups` migration.
- `speech_block_min_chars` is a preferred quality target; the engine maximum is
  a hard limit. Reviewed display text may exceed a TTS limit, while reviewed
  spoken text never does and is never duplicated.
- Unfinished-sentence continuation, complete-utterance packing, and maximum
  internal silence are independent policies. Explicit zero values are valid.
- The web workflow and legacy dubbing path now call the same speech-block
  implementation with the same settings.
- MOSS seam deduplication recognizes strong time-aligned three-word overlaps,
  keeps a later overlapping stream when it continues beyond an earlier suffix,
  and avoids word-by-word speaker flicker at those seams.
- Subtitle hard-silence and SaT boundary thresholds are explicit settings. SaT
  scores are gated by the configured threshold rather than always influencing
  the optimizer.
- Correction and LLM translation use explicit `full`, `overlap_only`, and
  `none` timing modes. Timing appears once per actionable cue, and `none`
  discloses neither time values nor overlap.

## Real Fedora evidence

Evaluation runs in an isolated git worktree using the installed Pandrator Pixi
environment and the configured application data. The live server checkout is
not modified.

Corpus: one real MOSS q8/Vulkan webinar transcription with CTC word alignment.

- Raw aligned words: 9,543
- Revised MOSS seam deduplication: 9,422 words (121 strong duplicate seam words
  removed)
- Adjacent speaker transitions after repair: 57
- Remaining negative adjacent word overlaps: 1
- SaT inference over 52,501 characters: about 20 seconds on the Fedora CPU
- SaT word-gap scores are strongly bimodal. At 0.15, 0.25, and 0.40 the cue
  layouts were identical, so the normal threshold range was not the cause of
  the fragmented German generation blocks.

Hard-silence comparison with all other subtitle defaults unchanged:

| Hard gap | Final cues | Median chars | Cues under 20 chars | Cues under 4 words | Cues spanning >1.5 s internal silence |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1,000 ms | 1,147 | 44 | 131 | 142 | 0 |
| 1,500 ms | 1,072 | 47 | 71 | 76 | 0 |
| 2,000 ms | 1,046 | 48 | 49 | 52 | 54 |

Conclusion: 1,500 ms is the best tested subtitle default. It removes much of
the 1,000 ms fragmentation without letting cues bridge pauses that are audibly
substantial. A 2,000 ms value remains useful as explicit high-continuity tuning
and as the default LLM editorial context threshold.

## Verification completed

- Focused subtitle, prompt, speech-block, MOSS, and audio-alignment suites:
  121 passed.
- Workflow, assembly, and foundation suites: 100 tests ran; three fingerprint
  compatibility assertions initially failed. Default timing context is now
  omitted from semantic fingerprints, like default single-request concurrency,
  so legacy artifacts remain reusable. The three affected tests pass after the
  fix.

## Release gate status

- Passive correction and German voiceover translation were completed through
  sequential MCP batches, including a correction revision over the translated
  track.
- Migration upgrade, source/result fencing, artifact lineage, hashes, timing
  preservation, and selection behavior were verified against the live test
  project.
- Final full-suite, frontend, distribution, and release verification is tracked
  by the 0.8.16 release process rather than duplicated in this historical
  implementation review.
