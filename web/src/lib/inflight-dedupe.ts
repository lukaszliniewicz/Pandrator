// Coalesce concurrent callers onto one in-flight promise per key.
//
// Inflight-only: once a request settles its slot is cleared, so sequential
// callers always refetch. This deliberately serves no stale data (voice and
// provider mutations stay immediately visible) while still collapsing the
// mount storm when several session-view components request the same
// catalogue at once.
//
// Error contract: sharers hold the original `request` promise and observe
// its outcome themselves. The internal cleanup runs on a derived promise
// with both handlers attached, so a rejection is never an unhandled
// detached rejection.
export function dedupeInflight<T>(
  slots: Map<unknown, Promise<T>>,
  key: unknown,
  start: () => Promise<T>,
  force = false
): Promise<T> {
  if (!force) {
    const existing = slots.get(key);
    if (existing) return existing;
  }
  const request = start();
  slots.set(key, request);
  const cleanup = () => {
    // Identity guard: a newer request (e.g. a forced refresh started while
    // this one was in flight) owns the slot; a late settler must not clear
    // it, and there is no cached value to overwrite either way.
    if (slots.get(key) === request) slots.delete(key);
  };
  void request.then(cleanup, cleanup);
  return request;
}
