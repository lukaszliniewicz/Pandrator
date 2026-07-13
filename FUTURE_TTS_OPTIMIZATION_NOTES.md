# Future TTS optimization and dialogue ideas

This file is deliberately left untracked. These are post-parity ideas, not part of the current web UI migration scope.

- Evaluate batching several adjacent generation segments into one LLM TTS-optimization request while preserving stable segment IDs and deterministic mapping back to each segment.
- Add a lightweight dialogue flag to generation segments. Dialogue can use shorter inter-segment and paragraph pauses than narration.
- Later, consider structured dialogue markup for multi-voice generation. Prefer a validated internal representation with an optional XML import/export form rather than making raw XML the authoritative editor state.
- Keep subtitle correction, translation, TTS text optimization, speech-block construction, and final subtitle layout as separate revisioned transformations.
- Measure whether batching reduces provider cost and latency without making partial regeneration, lineage, or review less reliable.
