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
- **Media edit** for transcript-guided video cuts, reviewable timing evidence,
  reversible edit revisions, and rendering.

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

## Media editing

For a media-edit session, `pandrator_plan_media_edit_workflow` returns a live,
read-only procedure. It inspects the session, workflow, media-edit readiness,
and STT settings before suggesting exactly one next action. The procedure is
ordered as source setup, STT settings, transcript or caption timing, media-edit
preparation, passive whole-recording proposal, human/model review, and render;
it can optionally finish by downloading the sole current `media_edit_media`
artifact, or by listing candidates when selection is ambiguous.

If no suitable session exists, create one with `workflow_kind=media_edit`;
`edit_media` is a supported included-stage value. Then either attach sources
yourself with `role=primary` and `role=transcript`, or pass approved source
references to the procedure planner so it returns the appropriate attachment
as its next action.

Use `transcript_mode=auto` to prefer an attached transcript (or a supplied
transcript source) and otherwise use ASR. In caption mode, the planner applies
the selected CTC, CTC-with-ASR-fallback, or ASR alignment settings. An attached
transcript remains authoritative: explicitly requesting ASR while captions are
attached is a blocking state, not permission to overwrite those captions.
The default `caption_alignment_ctc_model=auto` uses Pandrator's managed Canary
CTC aligner and does not load a whole-recording ASR model.

The passive proposal is never auto-approved. First call
`pandrator_list_media_edit_cuts` with the returned session and revision, then
call `pandrator_inspect_media_edit_boundary` for each relevant start/end edge.
Use `pandrator_refine_media_edit_boundary` for one edge at a time, then
re-list and re-inspect the changed revision before executing the planner's
gated `pandrator_update_media_edit(reviewed=true)` action. Only a reviewed
revision may be rendered. The planner is a live procedure rather than an
atomic snapshot; re-inspect after every action and monitor workflow plans or
durable work with their returned actions.

Recording and transcript files may be imported only from a named,
operator-approved local source root returned by
`pandrator_browse_local_sources`. Relative paths must be selected from that
browse result and may not escape the approved root.
