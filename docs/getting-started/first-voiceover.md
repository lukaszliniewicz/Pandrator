# Create your first voiceover

A voiceover session begins with timed source cues, optionally corrects and
translates them, converts the selected display and speech text into synthesis
blocks, generates reviewable takes, and aligns the selected audio to the source
timeline. Generated speech is not automatically a finished mix.

## 1. Prepare timed cues

Create a **Voiceover** session from source media or subtitles. Transcribe the
media or select an imported subtitle track, then review timing, speakers, and
text as described in [the subtitle workflow](first-subtitles.md).

Correct source-language cues before translation when errors would otherwise be
carried forward. If the target-language track needs stylistic cleanup, correct
that translation as another translation revision rather than relabeling it as
source-language correction.

## 2. Prepare text for speech

Display cues and TTS requests serve different purposes. Pandrator can keep
readable display text while creating a separate reviewed speech-text revision.
Use deterministic pronunciation entries for known names; use optional speech
optimization only when the provider needs a different spoken form.

Subtitle cues are then converted into speaker-safe speech blocks. A block may
combine nearby cues or split one long cue. Its source references and alignment
group preserve the relationship to the subtitle timeline.

## 3. Assign speakers and voices

Map each recognized speaker to a compatible provider voice, built-in voice, or
lawfully obtained reference voice. Check language support and listen to short
samples. A voice that works for one language or backend is not automatically
available to every service.

For mixed-language material, isolate a foreign phrase as a separate segment
when practical and use a per-segment language or alternate take. Literal tags
such as `[en]…[/en]` are not a portable inline language-switching protocol.

## 4. Generate and review takes

Generate a small representative section before the whole timeline. Check
pronunciation, pacing, emotion, speaker consistency, noise, and whether speech
can fit the source window without sounding rushed. Regenerate selected blocks
or create alternate takes rather than overwriting the subtitle track.

In **Block settings**, optionally enable **Reduce speech getting ahead of subtitles**.
After a complete generation run, Pandrator checks multi-cue blocks that finish
at least one second and 20% early by default, accounting for the configured start delay.
Open **Repair thresholds** to adjust these two limits, the minimum timing
improvement (1000 ms), and the minimum cue span per new block (1000 ms).
Both early-finish limits must be met; catch-up protection always applies.
It can split at a reliable sentence or clause boundary between source cues and
generate the two groups separately. Blocks carrying playback delay stay intact
so their shorter audio can help the voiceover catch up. Each original block is
considered once per pass. Uncertain text-to-cue mappings are skipped.

The repair uses additional TTS calls. A new plan version becomes active only
after replacement audio succeeds and passes timing checks; earlier versions
and takes remain available. Open **Speech plans** in the generation drawer to
browse versions, preview their blocks, and see whether each repair was applied,
not applied, failed, or stopped. Older repairs may have no recorded outcome.
This pass applies to complete voiceover generation
runs, including resumed runs, rather than individual selected-take regeneration.

## 5. Synchronize and mix

Pandrator maps selected takes through each speech block's source references.
Blocks in one alignment group are fitted together to their shared source
window. Review any speed-up, allowed start delay, and gaps between generated
sentences.

**Allow gentle voiceover slowdown (down to 0.9×)** in the session's audio
settings is a separate option, off by default. It preserves pitch and only uses
space within the block's own cue span. It skips small differences (less than
half a second or 5%) and any block already carrying delay. The existing maximum
catch-up speed remains in effect. Slowdown is applied during assembly, leaving
the generated take unchanged.

Choose deliberately among:

- original audio with generated speech mixed over it;
- source audio ducked under generated speech;
- generated dubbing without source audio; and
- subtitle-only or audio-only output when video rendering is unnecessary.

## 6. Export and inspect

Export audio, subtitle tracks, or rendered video. Video export can retain,
mix, or replace original audio and can use source, translated, bilingual, soft,
or burned subtitles where supported. Watch representative dialogue, silence,
speaker changes, music, and the final frames in the produced file.

## Quality checklist

- The selected subtitle revision is the intended source or target language.
- Every speaker has a compatible and legally usable voice.
- Display text and speech text differ only where deliberately reviewed.
- Speech blocks do not span inappropriate speakers or long internal silences.
- Difficult sections have reviewed takes before full generation.
- Alignment does not create unnatural speed or overlapping voices.
- Mix and subtitle choices match the intended audience.
- The rendered output was played outside Pandrator before delivery.

For the underlying data flow and synchronization parameters, see the
[subtitle-to-speech pipeline](../reference/subtitle-pipeline.md).
