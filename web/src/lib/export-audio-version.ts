import type {
  ExportAudioDescription,
  ExportAudioVersion,
  GenerationRun
} from './api-models';
import { apiTimestamp } from './run-metrics';

export type ExportAudioNote = {
  status: ExportAudioDescription['status'];
  headline: string;
  versions: string[];
};

/** A version in the page's words: the run's own label, when, which revision. */
export function exportAudioVersionLabel(
  version: ExportAudioVersion,
  runs: Pick<GenerationRun, 'id' | 'label'>[]
): string {
  const run = version.generation_run_id
    ? runs.find((item) => item.id === version.generation_run_id)
    : undefined;
  const runLabel = run?.label
    ? run.label
    : version.run_sequence_number != null
      ? `Run ${version.run_sequence_number}`
      : 'Assembled without a run';
  // Stored times without an offset are UTC, not the viewer's local time.
  const parsed = apiTimestamp(version.assembled_at);
  const when = !version.assembled_at
    ? ''
    : Number.isNaN(parsed)
      ? String(version.assembled_at)
      : new Date(parsed).toLocaleString();
  const revision =
    version.plan_revision_number != null
      ? `plan revision ${version.plan_revision_number}${version.active_revision ? ' (active)' : ''}`
      : '';
  return [runLabel, when ? `assembled ${when}` : '', revision]
    .filter(Boolean)
    .join(' · ');
}

/**
 * What the Output page says about the audio an export with no run would use.
 *
 * `selectableRunIds` are the runs the page's Audio version select offers; the
 * advice to select one is given only when a candidate is among them.
 */
export function exportAudioNote(
  description: ExportAudioDescription | null | undefined,
  runs: Pick<GenerationRun, 'id' | 'label'>[],
  selectableRunIds: string[] = runs.map((item) => item.id)
): ExportAudioNote | null {
  if (!description) return null;
  if (description.status === 'named' && description.version)
    return {
      status: 'named',
      headline: 'This export will use',
      versions: [exportAudioVersionLabel(description.version, runs)]
    };
  if (description.status === 'ambiguous' && description.candidates.length) {
    const selectable = description.candidates.some(
      (item) =>
        item.generation_run_id &&
        selectableRunIds.includes(item.generation_run_id)
    );
    const remove =
      'remove the assemblies you do not want under Intermediate audio assemblies, where each candidate is marked';
    return {
      status: 'ambiguous',
      headline: selectable
        ? `Several audio versions are available and none is singled out. Select a run under Audio version, or ${remove} (an assembly with no run can only be removed):`
        : `Several audio versions are available and none is singled out. To export, ${remove}:`,
      versions: description.candidates.map((item) =>
        exportAudioVersionLabel(item, runs)
      )
    };
  }
  if (description.status === 'none')
    return {
      status: 'none',
      headline:
        'No assembled audio exists yet. Assemble the current recordings in Generation before exporting.',
      versions: []
    };
  return null;
}
