type TimedCue = { start_ms: number; end_ms: number; speaker?: string | null };

type CueTimingOverlap = {
  overlap_ms: number;
  different_speakers: boolean;
};

/** Find positive overlaps, including nested cues, without changing cue order. */
export function findCueTimingOverlaps(cues: readonly TimedCue[]) {
  const overlaps = new Map<number, CueTimingOverlap>();
  const ordered = cues
    .map((cue, index) => ({
      start_ms: cue.start_ms,
      end_ms: cue.end_ms,
      speaker: cue.speaker,
      index
    }))
    .filter(
      (cue) =>
        Number.isFinite(cue.start_ms) &&
        Number.isFinite(cue.end_ms) &&
        cue.end_ms > cue.start_ms
    )
    .sort(
      (left, right) =>
        left.start_ms - right.start_ms || left.index - right.index
    );
  let previous: (typeof ordered)[number] | undefined;
  for (const cue of ordered) {
    if (previous && cue.start_ms < previous.end_ms) {
      const overlap_ms = Math.min(previous.end_ms, cue.end_ms) - cue.start_ms;
      const leftSpeaker = previous.speaker?.trim().toLowerCase();
      const rightSpeaker = cue.speaker?.trim().toLowerCase();
      const different_speakers = Boolean(
        leftSpeaker && rightSpeaker && leftSpeaker !== rightSpeaker
      );
      for (const index of [previous.index, cue.index]) {
        const existing = overlaps.get(index);
        overlaps.set(index, {
          overlap_ms: Math.max(existing?.overlap_ms ?? 0, overlap_ms),
          different_speakers: Boolean(
            existing?.different_speakers || different_speakers
          )
        });
      }
    }
    if (!previous || cue.end_ms > previous.end_ms) previous = cue;
  }
  return overlaps;
}
