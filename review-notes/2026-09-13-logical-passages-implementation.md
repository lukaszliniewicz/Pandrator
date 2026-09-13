# Logical passages: production integration contract

The requested change preserves meaningful source-linked text passages through
correction and translation, independently of display formatting. It does not
introduce audio alignment or a speech-duration ceiling.

## Semantic and timing rules

- Existing word timings may locate candidate source boundaries. Unmatched text
  must survive; without supported boundaries, retain the whole cue window.
- A model merge produces one atomic passage with the union source window.
  Original IDs remain provenance, not usable internal split points.
- The passage contract permits edits, adjacent merges and allowed deletions.
  It does not invent timing for model-requested subdivisions of one passage.
- TTS grouping remains the existing speaker/gap/character-based planner. Joining
  multiple surviving passages into one request retains their component anchors.
- Existing early repair, slowdown, delay catch-up and assembly algorithms remain
  in use. Their source references must still resolve against actual pinned
  Segment records, rather than fabricated subtitle ordinals.

## Integration chosen by the parent

Store accepted pre-display passage rows in versioned artifact metadata, bound to
the artifact's exact display revision and content hash. Correction and translation
prefer this representation when valid; initial source preparation uses existing
word evidence on a verified same-language, post-cut source path. Edits that do not
preserve a valid binding fall back to their actual selected cue records.

When speech planning needs a passage source, materialize those rows as a real,
non-active sibling DocumentRevision with Segment.node_kind='logical_passage'.
Pin the plan to that revision, preserving the existing timing lookup contract.
The display document's active revision remains its display representation.
Internal passage revisions must not appear as ordinary display-edit history.
Forked artifacts must rebind copied passage metadata to their new display
revision and discard any cached source-revision pointer from the original session.

New passive dispatch runs version their passage contract; already-pending runs
retain their saved input and response contract. Model-visible passage IDs and
original display cue IDs used by audio evidence must be distinguished explicitly.
Normal automatic LLM execution and DeepL input selection must also consume the
preserved passages. DeepL retains its per-input translation behavior. Normal
model checkpoint keys must distinguish the new contract from legacy outputs.

## Acceptance for this phase

1. Source text conservation and defensible timing on representative saved Pascal
   data and focused synthetic boundaries.
2. Correction/translation merges produce one combined timing window, with no
   retained executable internal boundary.
3. Display reflow does not change the preserved passage rows or resulting speech
   grouping for equivalent text.
4. Plan references resolve against the pinned logical revision; existing repair
   and catch-up checks pass using those records.
5. Legacy artifacts, pending dispatches, reviewed speech variants, edits and
   session forks retain valid behavior; no production session is rewritten.
6. Focused regression tests, Ruff, typing and local import-cycle/dead-code checks
   appropriate to changed modules. No TTS quality or deployment claim without
   performing that separate work.

The parent owns persistence, integration and model instructions. Luna/xhigh owns
only the explicitly assigned source-extraction core and bounded native model
contract work. Terra/high supplied the current-state backend trace.

## Implementation and validation

The checks below were completed in the development checkout before deployment.
They did not rewrite existing sessions or generated audio. They added no alignment
pass, model load or TTS generation.

The shared display projection is used by both passive dispatch and the normal
correction/translation handlers. Native LLM checkpoints include text, timing,
speaker and contract version. Restored translation groups are revalidated.
Uncertainty and audio-evidence ownership follow passage IDs, including when
source cues overlap. Supported manual subtitle review creates a new revision;
without a valid preserved ledger, subsequent processing uses that edited revision.
Existing reviewed `tts_optimized` artifacts keep their established display/speech
pairing.

The source extractor was run against the saved Pascal fixture: all 1,072 cues and
13,100 words produced 2,276 candidate passages with exact text conservation.
2,164 candidates used supported word boundaries; 112 retained cue windows.

Saved Sol/medium prototype responses were replayed through the production
correction and translation validators, with only their identifier/response
transport adapted. No model text was changed and no new model request was sent.

| Pascal excerpt | Source candidates | Corrected passages | German passages | Display cues, 40 / 60 columns | Default speech requests |
| --- | ---: | ---: | ---: | ---: | ---: |
| Opening | 37 | 23 | 23 | 26 / 22 | 9 |
| Previous long merge | 18 | 7 | 7 | 14 / 11 | 7 |
| Lecture | 13 | 8 | 8 | 14 / 11 | 6 |
| Previously repaired | 5 | 2 | 1 | 3 / 2 | 1 |

The final excerpt's 13,680 ms German merge remains one passage, with no assumed
internal repair boundary. Display reflow leaves the saved passages unchanged.
The speech counts use the existing planner's defaults and preserved speaker
metadata, not a new duration cap.
Reproduction inputs and scripts remain under `tmp/voiceover-anchor-prototype/`;
the session transcript is not part of this source change.

Validation performed:

- `pytest` on the native correction/translation, source passages, logical web
  integration, web/MCP dispatch, dispatch context, workflow handlers, session
  forks, voiceover repair, early repair and audio-sync suites: **300 passed**.
- After the overlap-ownership fix, reran logical web integration and web/MCP
  dispatch, including the new overlap regression: **48 passed**. There are
  **301 distinct passing cases** across these runs.
- `ruff check` on all changed Python files and `git diff --check`: passed.
- `mypy --follow-imports=skip` on the three new modules and both native language
  modules: passed. Wider dispatch/workflow checks have the same 24 diagnostic
  messages as HEAD; these pre-existing errors and missing dependency stubs were
  not expanded into this feature.
- `vulture --min-confidence 100` on the changed backend implementation: no
  findings. Pydeps and a local AST import graph found no cycle involving the new
  modules.

Terra/xhigh independently reviewed the native contract and web integration.
The parent fixed its two concrete findings: stale speaker-dependent checkpoint
reuse, and uncertainty incorrectly copied to temporally overlapping passages.
The remaining untested claim is listening quality with freshly generated audio;
this work verifies representation, timing ownership and workflow behavior.
