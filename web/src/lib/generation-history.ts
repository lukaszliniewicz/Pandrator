import type { GenerationRun } from './api-models';

export type SecondPassKind = 'regroup' | 'repair' | 'mixed' | 'unknown';

type TimingRepair = NonNullable<GenerationRun['timing_repair']>;
type TimingVersion = TimingRepair['versions'][number];

const REGROUP_OPERATION_REASON = 'passage_regroup';
const REPAIR_OPERATION_REASON = 'early_timing_repair';
// repair_status values that definitively did not change the audio. Pending
// attempts are still in flight and 'unknown' (old API) proves nothing: neither
// may count as rejected.
const REJECTED_STATUSES = new Set(['not_applied', 'failed', 'stopped']);

function versionOperationReason(version: TimingVersion): string | null {
  const raw = version.reason ?? null;
  if (typeof raw !== 'string') return null;
  const normalized = raw.trim().toLowerCase();
  return normalized ? normalized : null;
}

function snapshotSecondPass(run: GenerationRun): SecondPassKind | null {
  const direct = (run.second_pass ?? '').trim().toLowerCase();
  if (direct === 'regroup') return 'regroup';
  if (direct === 'repair') return 'repair';
  const tts = run.settings_snapshot?.tts;
  if (tts && typeof tts === 'object' && !Array.isArray(tts)) {
    const mode = String(
      (tts as Record<string, unknown>).speech_block_generation_mode ?? ''
    )
      .trim()
      .toLowerCase();
    const regroupEnabled =
      (tts as Record<string, unknown>).speech_block_regroup_enabled === true;
    const repairEnabled =
      (tts as Record<string, unknown>).speech_block_early_repair_enabled ===
      true;
    if (mode === 'passage' && regroupEnabled) return 'regroup';
    if (mode === 'legacy' && repairEnabled) return 'repair';
  }
  return null;
}

export function secondPassKind(run: GenerationRun): SecondPassKind {
  const repair = run.timing_repair;
  if (!repair) return 'unknown';
  // Explicit top-level kind from the serializer (persisted operation reasons;
  // snapshot fallback only when zero children) is authoritative.
  const explicit = (repair.kind ?? '').trim().toLowerCase();
  if (
    explicit === 'regroup' ||
    explicit === 'repair' ||
    explicit === 'mixed' ||
    explicit === 'unknown'
  ) {
    return explicit;
  }
  const operationKinds = new Set<SecondPassKind>();
  for (const version of repair.versions ?? []) {
    const reason = versionOperationReason(version);
    if (!reason) continue;
    if (reason === REGROUP_OPERATION_REASON) operationKinds.add('regroup');
    else if (reason === REPAIR_OPERATION_REASON) operationKinds.add('repair');
    else if (reason === 'mixed') operationKinds.add('mixed');
  }
  if (
    operationKinds.has('mixed') ||
    (operationKinds.has('regroup') && operationKinds.has('repair'))
  ) {
    return 'mixed';
  }
  if (operationKinds.size === 1) {
    return operationKinds.has('regroup') ? 'regroup' : 'repair';
  }
  // Old API without kind/reasons: fall back to the immutable root snapshot
  // (second_pass, then saved mode+enabled). Per-attempt outcome strings such
  // as duration_misfit or added_delay never decide the kind, and counts never
  // do either.
  return snapshotSecondPass(run) ?? 'unknown';
}

export function historyCounts(repair: TimingRepair) {
  const versions = repair.versions ?? [];
  const attempted = versions.length;
  const applied = versions.filter((v) => v.repair_status === 'applied').length;
  const rejected = versions.filter((v) =>
    REJECTED_STATUSES.has(v.repair_status)
  ).length;
  const pending = versions.filter((v) => v.repair_status === 'pending').length;
  return { attempted, applied, rejected, pending };
}

function plural(count: number, singular: string, pluralForm?: string) {
  return count === 1 ? singular : (pluralForm ?? `${singular}s`);
}

export function historyHeading(run: GenerationRun): string {
  const repair = run.timing_repair;
  if (!repair) return '';
  const kind = secondPassKind(run);
  const { attempted, applied } = historyCounts(repair);
  if (repair.status === 'running') {
    if (kind === 'regroup') return 'Combining passages';
    if (kind === 'repair') return 'Repairing timing';
    return 'Running second pass';
  }
  if (!attempted) {
    // 'No eligible groups' only with explicit evidence; otherwise neutral.
    if (kind === 'regroup' && repair.no_eligible_groups)
      return 'No eligible groups';
    if (kind === 'repair') return 'No split was needed';
    if (kind === 'mixed') return 'No second-pass changes';
    return 'No groups regenerated';
  }
  if (kind === 'regroup')
    return `${applied} ${plural(applied, 'group')} merged`;
  if (kind === 'repair') return `${applied} ${plural(applied, 'block')} split`;
  if (attempted && applied === 0) return 'No second-pass changes';
  return `${applied} ${plural(applied, 'adjustment')} accepted`;
}

export function historyHelper(run: GenerationRun): string {
  const repair = run.timing_repair;
  if (!repair) return '';
  const kind = secondPassKind(run);
  const { attempted, applied, rejected } = historyCounts(repair);
  if (repair.status === 'running') {
    if (kind === 'regroup')
      return 'Accepted groups become part of this run as they finish.';
    if (kind === 'repair')
      return 'Accepted splits become part of this run as they finish.';
    return 'Accepted second-pass results become part of this run as they finish.';
  }
  if (repair.status === 'stopped' || repair.status === 'failed') {
    if (kind === 'regroup')
      return 'Regroup stopped. The last accepted audio remains available.';
    if (kind === 'repair')
      return 'Timing repair stopped. The last accepted audio remains available.';
    return 'Second pass stopped. The last accepted audio remains available.';
  }
  if (!attempted) {
    if (kind === 'regroup' && repair.no_eligible_groups)
      return 'No adjacent passage groups were eligible. The original audio is retained.';
    if (kind === 'repair')
      return 'No split was needed. The original audio is retained.';
    return 'No groups were regenerated. The original audio is retained.';
  }
  if (kind === 'regroup')
    return `${attempted} ${plural(attempted, 'group')} regenerated: ${applied} accepted, ${rejected} rejected.`;
  if (kind === 'repair')
    return 'Automatic timing repairs are saved together in this run.';
  return `${attempted} second-pass ${plural(attempted, 'attempt')} : ${applied} accepted, ${rejected} rejected.`;
}

export function historyAriaLabel(run: GenerationRun): string {
  const kind = secondPassKind(run);
  if (kind === 'regroup') return 'Passage regroup history';
  if (kind === 'repair') return 'Timing repair history';
  return 'Second-pass history';
}

export function historyDetailsSummary(run: GenerationRun): string {
  const repair = run.timing_repair;
  if (!repair) return '';
  const kind = secondPassKind(run);
  const { attempted } = historyCounts(repair);
  // The prefix names the pass without claiming merge for mixed/unknown.
  if (kind === 'regroup')
    return `Regroup details · ${attempted} ${attempted === 1 ? 'group' : 'groups'}`;
  if (kind === 'repair')
    return `Repair details · ${attempted} ${attempted === 1 ? 'attempt' : 'attempts'}`;
  return `Second-pass details · ${attempted} ${attempted === 1 ? 'attempt' : 'attempts'}`;
}

export function historyVersionTitle(run: GenerationRun, index: number): string {
  const kind = secondPassKind(run);
  if (kind === 'regroup') return `Group ${index + 1}`;
  if (kind === 'repair') return `Repair ${index + 1}`;
  return `Attempt ${index + 1}`;
}

export function historyInspectLabel(run: GenerationRun, index: number): string {
  const kind = secondPassKind(run);
  if (kind === 'regroup') return `Inspect group ${index + 1} audio`;
  if (kind === 'repair') return `Inspect repair ${index + 1} audio`;
  return `Inspect attempt ${index + 1} audio`;
}

export function historyVersionOutcome(run: GenerationRun, status: string) {
  const kind = secondPassKind(run);
  const applied =
    kind === 'regroup'
      ? 'Merge applied'
      : kind === 'repair'
        ? 'Split applied'
        : 'Adjustment applied';
  const failed =
    kind === 'regroup'
      ? 'Merge failed'
      : kind === 'repair'
        ? 'Repair failed'
        : 'Attempt failed';
  return (
    {
      applied,
      not_applied: 'Kept previous audio',
      pending: 'Checking timing',
      stopped: 'Stopped',
      failed
    }[status] ?? 'Outcome unavailable'
  );
}

export function historyVersionReason(value?: string | null) {
  return (
    {
      added_delay: 'The split would have added delay.',
      missed_repair_anchor: 'The repair anchor was no longer available.',
      duration_misfit:
        'The regenerated group did not fit its timing window, so the original audio was kept.',
      selection_changed: 'The selected plan or audio changed.',
      generation_stopped: 'Generation was stopped.',
      generation_failed: 'Generation failed.',
      active_plan_changed: 'The active plan changed.',
      invalid_regroup_settings: 'The saved regroup settings were invalid.'
    }[value ?? ''] ?? ''
  );
}

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
  const kind = secondPassKind(run);
  // A running second pass names the pass even before anything is accepted;
  // applied_count must not gate this, or initial regroup keeps saying
  // 'Repairing timing'.
  if (run.phase === 'repairing_timing' || repair?.status === 'running') {
    if (kind === 'regroup') return 'Combining passages';
    if (kind === 'repair') return 'Repairing timing';
    if (repair) return 'Running second pass';
    return status;
  }
  if (!repair?.applied_count) return status;
  if (kind === 'regroup')
    return `${status} · ${repair.applied_count} ${repair.applied_count === 1 ? 'group' : 'groups'} merged`;
  if (kind === 'mixed' || kind === 'unknown')
    return `${status} · ${repair.applied_count} ${repair.applied_count === 1 ? 'adjustment' : 'adjustments'} accepted`;
  return `${status} · ${repair.applied_count} ${repair.applied_count === 1 ? 'block' : 'blocks'} split`;
}
