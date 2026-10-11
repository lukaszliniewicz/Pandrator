import type { GenerationSegment } from './api-models';
import type { PlayableTake } from './generation-view-models';

export type TakeSelection = {
  historical: boolean;
  audioVersion: boolean;
  sequence: number;
  sequences: Record<string, number>;
};

export function selectedGenerationTake(
  item: GenerationSegment,
  selection: TakeSelection
): PlayableTake | undefined {
  const playable = (
    take: GenerationSegment['takes'][number]
  ): take is PlayableTake => Boolean(take.artifact_id);
  const takes = item.takes ?? [];
  if (selection.historical) {
    if (selection.audioVersion)
      return takes.find(
        (take): take is PlayableTake =>
          take.id === item.history_take_id && playable(take)
      );
    const candidates = takes
      .filter(
        (take): take is PlayableTake =>
          playable(take) &&
          ['completed', 'stale'].includes(take.status) &&
          Boolean(take.generation_run_id) &&
          (selection.sequences[take.generation_run_id ?? ''] ?? Infinity) <=
            selection.sequence
      )
      .sort(
        (left, right) =>
          (selection.sequences[right.generation_run_id ?? ''] ?? 0) -
            (selection.sequences[left.generation_run_id ?? ''] ?? 0) ||
          String(right.created_at).localeCompare(String(left.created_at))
      );
    if (candidates.length) return candidates[0];
    return (
      takes.find(
        (take): take is PlayableTake =>
          !take.generation_run_id && take.is_active && playable(take)
      ) ??
      takes.find(
        (take): take is PlayableTake =>
          !take.generation_run_id && playable(take)
      )
    );
  }
  return (
    takes.find(
      (take): take is PlayableTake => take.is_active && playable(take)
    ) ??
    takes.find(
      (take): take is PlayableTake => !take.generation_run_id && playable(take)
    )
  );
}
