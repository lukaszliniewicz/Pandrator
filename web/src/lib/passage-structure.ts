export type PassageTextLayer = 'display' | 'speech';

export type PassageBoundary = {
  id: string;
  offset: number;
  display_offset: number | null;
  speech_offset: number | null;
  left_reference: string | number;
  right_reference: string | number;
  left_end_ms: number;
  right_start_ms: number;
  gap_ms: number;
  left_window: [number, number];
  right_window: [number, number];
  natural: boolean;
  boundary_kind: 'sentence' | 'clause' | 'comma' | 'conjunction' | null;
  warning: string | null;
  split_allowed: boolean;
  split_blocked_reason: string | null;
};

export type PassageLayer = {
  status: 'mapped' | 'stale' | 'unavailable';
  text?: string;
  message: string | null;
  boundaries: PassageBoundary[];
};

export type PassageStructure = {
  schema_version: number;
  layers: Record<PassageTextLayer, PassageLayer>;
};

export function passageTime(milliseconds: number): string {
  const value = Math.max(0, Math.round(milliseconds));
  const hours = Math.floor(value / 3_600_000);
  const minutes = Math.floor((value % 3_600_000) / 60_000);
  const seconds = Math.floor((value % 60_000) / 1000);
  return `${hours ? `${hours}:` : ''}${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}.${String(value % 1000).padStart(3, '0')}`;
}

export function boundaryLabel(boundary: PassageBoundary): string {
  const pause =
    boundary.gap_ms > 0
      ? `${(boundary.gap_ms / 1000).toFixed(2)} s source pause`
      : boundary.gap_ms < 0
        ? `${(-boundary.gap_ms / 1000).toFixed(2)} s overlap`
        : 'No source gap';
  const quality = boundary.natural
    ? `${boundary.boundary_kind ?? 'Natural'} boundary`
    : 'Inside an unfinished phrase';
  return `Passages ${boundary.left_reference} / ${boundary.right_reference}: ${passageTime(boundary.left_end_ms)} to ${passageTime(boundary.right_start_ms)}. ${pause}. ${quality}. ${boundary.warning ?? ''} ${boundary.split_blocked_reason ?? 'Inspect split.'}`.trim();
}

/** Python offsets count Unicode code points, not JavaScript UTF-16 units. */
export function passagePieces(text: string, layer?: PassageLayer) {
  const points = Array.from(text);
  if (!layer || layer.status !== 'mapped' || layer.text !== text) return null;
  let previous = 0;
  const pieces: { text: string; boundary: PassageBoundary | null }[] = [];
  for (const boundary of layer.boundaries) {
    if (
      !Number.isInteger(boundary.offset) ||
      boundary.offset <= previous ||
      boundary.offset >= points.length
    )
      return null;
    pieces.push({
      text: points.slice(previous, boundary.offset).join(''),
      boundary
    });
    previous = boundary.offset;
  }
  pieces.push({ text: points.slice(previous).join(''), boundary: null });
  return pieces;
}
