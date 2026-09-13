import type { GenerationRun } from './api-models';

export function visibleGenerationRuns(runs: GenerationRun[]) {
  return runs.filter(
    (run) => !run.output_generation_run_id && !run.early_repair_parent_run_id
  );
}

// History keeps a stable root selection; audio operations use the immutable
// version's own plan and take sequence ceiling.
export function generationHistoryVersion(
  runs: GenerationRun[],
  rootId: string,
  versionId = ''
): GenerationRun | null {
  const root = runs.find((run) => run.id === rootId);
  if (!root) return null;
  const repair = root.timing_repair;
  const requested =
    versionId === root.id ||
    repair?.versions.some((version) => version.generation_run_id === versionId)
      ? versionId
      : (repair?.result_generation_run_id ?? root.id);
  return runs.find((run) => run.id === requested) ?? root;
}

export function generationHistoryStatus(run: GenerationRun) {
  const repair = run.timing_repair;
  const status =
    run.phase === 'repairing_timing' ? 'Repairing timing' : run.status;
  return repair?.applied_count
    ? `${status} · ${repair.applied_count} ${repair.applied_count === 1 ? 'block' : 'blocks'} split`
    : status;
}
