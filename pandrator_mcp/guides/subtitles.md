# Subtitle workflow

A subtitle session operates on an audio or video source:

1. **Transcribe** creates timed source-language cues.
2. **Correct** creates a distinct revision for punctuation, wording, cue merges,
   and splits without translating.
3. **Translate** creates a target-language revision and retains lineage to the
   source or corrected cues.
4. **Preview** compares source, correction, and translation selections.
5. **Export** writes selected cues as SRT or VTT, or concatenates a transcript.

Correction and translation are separate review decisions. Inspect the workflow
snapshot and selected artifact IDs before continuing downstream. Selecting an
older upstream revision can invalidate or clear dependent selections; use the
reported impact rather than assuming downstream work is still current.

Speech recognition may run locally or through a provider depending on the
configured engine. Translation may use an LLM or a translation provider. An
exact plan should disclose remote data transfer and identify the selected input
revision.
