import type { DocumentRecord, WorkflowSnapshot } from './api-models';

/** Choose written captions by their selected artifact, never by cross-document revision numbers. */
export function selectedDisplayArtifact(
  documents: DocumentRecord[],
  workflow: WorkflowSnapshot
): string {
  const candidates = documents.flatMap((document) =>
    document.revisions
      .filter((revision) => revision.artifact?.state !== 'deleted')
      .map((revision) => ({ document, revision }))
  );
  const artifactIds = new Set(
    candidates.map(({ revision }) => revision.artifact?.id).filter(Boolean)
  );
  for (const key of ['translate', 'correct', 'transcribe']) {
    const stage = workflow.stages.find((item) => item.key === key);
    const selected = stage?.selected_artifact_id ?? stage?.artifact?.id;
    if (selected && artifactIds.has(selected)) return selected;
  }
  const imported = workflow.subtitle_source?.subtitle_artifact_id;
  if (imported && artifactIds.has(imported)) return imported;

  const rank = new Map([
    ['translation', 3],
    ['correction', 2],
    ['transcription', 1]
  ]);
  return (
    candidates
      .filter(
        ({ document, revision }) =>
          rank.has(document.stage) &&
          revision.id === document.active_revision_id &&
          revision.artifact
      )
      .sort(
        (left, right) =>
          (rank.get(right.document.stage) ?? 0) -
            (rank.get(left.document.stage) ?? 0) ||
          right.revision.created_at.localeCompare(left.revision.created_at) ||
          right.revision.id.localeCompare(left.revision.id)
      )[0]?.revision.artifact?.id ?? ''
  );
}
