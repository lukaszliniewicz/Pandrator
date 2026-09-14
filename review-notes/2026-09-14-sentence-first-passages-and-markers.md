# Sentence-first source passages and inspectable generation composition

## Policy implemented

Source-passage defaults are a soft minimum of 60 characters, a preferred
sentence window of 160 characters, and 20 characters of sentence look-ahead.
These are source-language passage preferences, not TTS request limits.

The builder records candidates rather than cutting at the first comma or size
threshold. A sentence ending within the preference window plus look-ahead wins
against an earlier comma. Short complete sentences remain independent. Without
such a sentence, it chooses a substantial clause within the window, preferring
stronger clause punctuation and the earlier candidate among equal choices. A
small dependent tail, dangling function word, bracketed span, or recognized
named apposition is not a good fallback. When none is acceptable, it continues
to a natural boundary and records that the length preference was exceeded.
This is a conservative language heuristic, not full syntactic parsing.

Verified same-speaker cue containers are considered together before selecting
boundaries. Imported cue formatting no longer dictates a split at every seam.
Confidence, ownership, overlaps, speaker changes, bounded cumulative hesitation
and the existing 30-second cross-cue envelope guard remain independent safety
checks. Unresolved source seams are flagged. A word without trustworthy timing
is not assigned an invented timestamp, and incomplete evidence retains its cue
window. The full transcript wording and original word-time ledger are untouched.

The web adapter now forwards TimedWord.confidence into the passage builder.
Known lexical matches take priority over temporal fallback; an unclaimed word
from an old cue association may still match its actual text, but the same word
ID cannot establish two independent passage anchors.

## Timed-passage dots

The generation drawer Display menu can show verified passage boundaries in both
the table and reading views. Dots are interface-only elements, not characters
inserted into saved text or TTS input. They expose source references, source
windows, gaps/overlaps and boundary warnings. Clicking opens a split preview;
a potentially unnatural split requires deliberate confirmation. New children
need synthesis, while the original take remains in history.

Markers require complete current text-to-passage correspondence. Changed text,
ambiguous/overlapping spans and unavailable companion-layer correspondence fail
closed. Merged passages stay merged: ancestral IDs do not recreate discarded
internal timing. A marker-bound split is revalidated by the backend and uses
verified display/spoken offsets, never proportional companion offsets.

## Real-data replay

Input: connector-exported Pascal artifact 77abeb5d-5361-4a46-982e-82b003ba6c0a.
SHA-256: e346c3ae1f1b099a4d7ec73c4622e763307d6c273ecb92409f7157f3907cc0b8.
The input has 1,072 cues and 13,100 timed words.

The deployed baseline produced 2,276 source passages; the new builder produced
1,553. There are 140 cross-cue passages, 13 passages above 160 characters, and
the longest is 207 characters. Median length is 42: the minimum deliberately
does not force short complete sentences to merge. Thirty-nine unresolved source
seams remain conservatively flagged rather than claiming perfect syntax.

The adjective-noun example becomes one source passage:
"Bloom helped transform a local religious controversy into a public national issue."
Its measured window is 759,780 to 769,060 ms. The source transcript's spelling is
retained; this is segmentation, not correction.

Assertions verified identical source wording/order, retention of every previously
used word ID, no duplicate timing claims, and unchanged measured word endpoints.
Three duplicated timing references in the old temporal fallback are no longer
claimed twice. The exported input's hash remains unchanged. Replay: about 0.6 s.
Outputs and executable replay are in tmp/sentence-policy-review/.

## Verification and existing sessions

374 focused backend regressions passed. Frontend check: zero errors/warnings;
production build succeeded. Eight passage-marker browser/helper checks and
fourteen edit/regeneration checks passed across Chromium and Firefox. Test-server
logs include a pre-existing unavailable Silero model catalogue (404); these tests
do not use that service.

Existing stored correction/translation passage ledgers are authoritative and
are not silently rebuilt with the new segmentation policy. The current TTS
planning cap is unchanged. This change does not regenerate or delete audio,
replace the active Pascal plan, or run correction/translation models. To apply
the new initial segmentation end-to-end to existing material requires an explicit
new processing branch, rather than overwriting reviewed derivatives.
