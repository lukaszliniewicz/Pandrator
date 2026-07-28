# Pandrator workflows

Pandrator organizes creative work in sessions. A session fixes the workflow
kind, languages, included stages, settings revisions, source artifacts, and
selected outputs. A stage reads a selected artifact and creates a new artifact;
it does not silently overwrite an earlier revision.

The supported workflow kinds are:

- **Audiobook** for document cleanup, narration segmentation, optional
  text-to-speech optimization, generation, assembly, and export.
- **Subtitles** for transcription, correction, optional translation, comparison,
  and subtitle or transcript export.
- **Voiceover** for the subtitle pipeline plus generated speech, assembly or
  mixing, and media export. The underlying application also calls this the
  dubbing pipeline.

Use an inspect-first sequence:

1. Inspect capabilities, providers, and voices.
2. Inspect existing sessions before creating another with the same purpose.
3. Inspect the session and workflow snapshot.
4. Review source and selected artifact revisions.
5. Preview an exact workflow plan, including provider disclosures.
6. Ask the user to approve that exact plan.
7. Execute once and observe the returned work reference.
8. Review generated artifacts before selecting, assembling, or exporting them.

Planning and execution are separate on purpose. A plan becomes stale when a
relevant session, source, setting, provider, or selected artifact changes.
Re-plan instead of attempting to work around a stale-plan failure.
