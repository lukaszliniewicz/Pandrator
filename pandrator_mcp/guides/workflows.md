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

For an unfamiliar request, start with `pandrator_recommend_next_steps`, then
read this guide and the workflow-specific guide it identifies. Use this
inspect-first sequence:

1. Inspect target status and capabilities. Target status reports requested and
   granted application scopes plus the names of approved local roots.
2. Inspect existing sessions before creating another with the same purpose.
3. For a sidecar-host file, browse an approved named root and import only the
   returned relative path. Import automatically resumes byte transfer and
   attaches the immutable source with the inspected session revision.
4. Inspect the session, workflow snapshot, source, and selected artifact
   revisions.
5. Use a passive dispatcher when the MCP host model should perform correction,
   translation, document cleanup, or speech optimization. Claim one packet,
   submit every required ID exactly once, and continue sequentially.
6. Before speech generation, inspect the live TTS catalog and apply exact
   service, model, and voice IDs to the current TTS settings revision. User
   examples are not guaranteed catalog identifiers.
7. Preview an exact workflow plan, including provider disclosures.
8. Ask the user to approve every confirmation required by that exact plan.
9. Execute once and poll the returned durable work reference to terminal.
10. Review generation runs and artifacts. Create one typed export plan per
    requested output variant, execute it, and poll it to terminal.
11. Download requested immutable artifacts to the approved local output root
    and report both artifact IDs and local paths.

Planning and execution are separate on purpose. A plan becomes stale when a
relevant session, source, setting, provider, or selected artifact changes.
Re-plan instead of attempting to work around a stale-plan failure.

The model never chooses a filesystem root, connection origin, upload chunk
size, credential, or download transport. Those are sidecar/operator policy.
Expected tool failures are typed `isError` results; inspect their code and
retryability instead of guessing from prose.
