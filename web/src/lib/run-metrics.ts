/** SQLite dates without an offset are UTC, just like the API's aware dates. */
export function apiTimestamp(value: string | null | undefined): number {
  if (!value) return NaN;
  return Date.parse(
    /(?:Z|[+-]\d{2}:?\d{2})$/i.test(value) ? value : `${value}Z`
  );
}

export function formatRunDuration(seconds: number): string {
  const rounded = Math.max(0, Math.round(seconds));
  if (rounded < 60) return `${rounded}s`;
  const minutes = Math.floor(rounded / 60);
  if (minutes < 60)
    return `${minutes}m${rounded % 60 ? ` ${rounded % 60}s` : ''}`;
  return `${Math.floor(minutes / 60)}h${minutes % 60 ? ` ${minutes % 60}m` : ''}`;
}

/** A deliberately approximate projection from this run's elapsed time/progress. */
export function estimatedRemaining(
  elapsed: number | null,
  progress: number | null | undefined
): string {
  if (progress != null && progress >= 1) return 'Finishing…';
  if (
    elapsed == null ||
    !Number.isFinite(elapsed) ||
    elapsed < 10 ||
    progress == null ||
    !Number.isFinite(progress) ||
    progress < 0.01
  )
    return 'Estimating…';
  const seconds = (elapsed * (1 - progress)) / progress;
  if (seconds < 60) return 'Under a minute';
  // Minute precision avoids suggesting a second-by-second promise.
  return `About ${formatRunDuration(Math.round(seconds / 60) * 60)}`;
}

export function formatRunCost(cost: number | null): string {
  if (cost == null) return 'not metered';
  if (cost === 0) return '$0.00';
  return cost < 0.01 ? `$${cost.toFixed(6)}` : `$${cost.toFixed(4)}`;
}
