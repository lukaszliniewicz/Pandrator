import type {
  ExportAudioDescription,
  ExportAudioVersion,
  GenerationRun
} from './api-models';

export type ExportAudioNote = {
  status: ExportAudioDescription['status'];
  headline: string;
  versions: string[];
};

/** A version in the page's words: the run's own label, when, which revision. */
function exportAudioVersionLabel(
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
  const parsed = new Date(version.assembled_at);
  const when = !version.assembled_at
    ? ''
    : Number.isNaN(parsed.getTime())
      ? String(version.assembled_at)
      : parsed.toLocaleString();
  const revision =
    version.plan_revision_number != null
      ? `plan revision ${version.plan_revision_number}${version.active_revision ? ' (active)' : ''}`
      : '';
  return [runLabel, when ? `assembled ${when}` : '', revision]
    .filter(Boolean)
    .join(' · ');
}

/** What the Output page says about the audio an export with no run would use. */
export function exportAudioNote(
  description: ExportAudioDescription | null | undefined,
  runs: Pick<GenerationRun, 'id' | 'label'>[]
): ExportAudioNote | null {
  if (!description) return null;
  if (description.status === 'named' && description.version)
    return {
      status: 'named',
      headline: 'This export will use',
      versions: [exportAudioVersionLabel(description.version, runs)]
    };
  if (description.status === 'ambiguous' && description.candidates.length)
    return {
      status: 'ambiguous',
      headline:
        'Several audio versions are available and none is singled out. Select a run under Audio version, or remove the assemblies you do not want under Intermediate audio assemblies (an assembly with no run can only be removed):',
      versions: description.candidates.map((item) =>
        exportAudioVersionLabel(item, runs)
      )
    };
  if (description.status === 'none')
    return {
      status: 'none',
      headline:
        'No assembled audio exists yet. Assemble the current recordings in Generation before exporting.',
      versions: []
    };
  return null;
}
