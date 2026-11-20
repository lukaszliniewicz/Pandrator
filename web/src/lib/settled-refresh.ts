// Run a refresh only after changes have settled.
//
// `change(ready)` reports the latest state: the first ready call after a
// not-ready one runs at once, later ones restart a trailing timer so only the
// last change in a burst runs. `settle(ms)` schedules after its own interval,
// `now()` runs at once, and every path drops a pending run first.
export interface SettledRefresh {
  change(ready: boolean): void;
  settle(ms?: number): void;
  now(): void;
  cancel(): void;
}

export function settledRefresh(
  run: () => void,
  options: { settleMs: number; onwait?: () => void }
): SettledRefresh {
  let timer: ReturnType<typeof setTimeout> | undefined;
  let wasReady = false;
  const cancel = () => {
    clearTimeout(timer);
    timer = undefined;
  };
  const now = () => {
    cancel();
    run();
  };
  const settle = (ms = options.settleMs) => {
    cancel();
    options.onwait?.();
    timer = setTimeout(now, ms);
  };
  return {
    change(ready) {
      const previous = wasReady;
      wasReady = ready;
      if (!ready) cancel();
      else if (!previous) now();
      else settle();
    },
    settle,
    now,
    cancel
  };
}
