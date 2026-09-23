const COMPLETED_DISCLOSURE_EXCLUDED_KEYS = new Set([
  'generate_audio',
  'export',
  'output',
  'edit_media',
  'preview'
]);

export type CollapsibleStageLike = {
  key?: string | null;
  status?: string | null;
  detail?: string | null;
  artifact?: unknown | null;
  artifacts?: { id?: string | null }[] | null;
  selected_artifact_id?: string | null;
  progress?: number | null;
};

function hasSelectedArtifact(
  stage: CollapsibleStageLike | null | undefined
): boolean {
  if (!stage) return false;
  if (stage.artifact != null) return true;
  const selectedId = stage.selected_artifact_id;
  if (!selectedId) return false;
  const history = stage.artifacts;
  return (
    Array.isArray(history) && history.some((entry) => entry?.id === selectedId)
  );
}

export function isCollapsibleCompletedStage(
  stage: CollapsibleStageLike | null | undefined,
  options: { captionUnreliable?: boolean } = {}
): boolean {
  if (!stage) return false;
  if (COMPLETED_DISCLOSURE_EXCLUDED_KEYS.has(String(stage.key ?? '')))
    return false;
  if (stage.status !== 'completed') return false;
  if (typeof stage.detail === 'string' && stage.detail.trim()) return false;
  if (!hasSelectedArtifact(stage)) return false;
  if (stage.progress != null) return false;
  if (options.captionUnreliable) return false;
  return true;
}

export function disclosureIdentity(
  sessionId: string | null | undefined,
  stageKey: string | null | undefined
): string {
  return `${sessionId ?? ''}::${stageKey ?? ''}`;
}

export function effectiveDisclosureExpanded(
  userExpanded: boolean,
  collapsibleNow: boolean
): boolean {
  return userExpanded || !collapsibleNow;
}
