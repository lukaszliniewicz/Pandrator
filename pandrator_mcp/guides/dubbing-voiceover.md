# Voiceover and dubbing workflow

Pandrator stores this as a `voiceover` session and uses the dubbing processing
pipeline. It begins with transcription, correction, and optional translation,
then adds:

- optional whole-document or generation-time speech optimization;
- per-segment speech generation with reviewable takes;
- deliberate take selection and audio assembly;
- optional synchronization, source-audio ducking, or a dubbing-only mix; and
- export of audio, subtitle tracks, or rendered video.

Generated segments are not automatically a finished mix. Inspect selected
voices, provider readiness, timing constraints, and output mode first. Source
media, subtitle text, and voice samples may be sensitive; a workflow plan should
say which external providers receive each kind of data.

For subtitle-only outcomes, use a subtitle session and stop before speech
generation. For narration without timed media, use an audiobook session.
