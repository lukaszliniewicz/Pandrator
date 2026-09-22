import { errorMessage } from './errors';
import type { LoadState } from './api-models';

export class ResourceState<T> {
  value = $state<T>(undefined as T);
  status = $state<LoadState>('idle');
  error = $state('');
  private pending?: Promise<T>;
  // Generation counter: every reset() and every newly started request bumps
  // it. A completion from an older generation (orphaned by reset() or by a
  // forced reload) must not overwrite newer state.
  private epoch = 0;
  // Set when markStale() arrives while a request is in flight. The resolver
  // leaves status 'stale' so callers know a trailing reload is needed.
  private invalidatedWhilePending = false;

  constructor(initial: T) {
    this.value = initial;
  }

  get loading() {
    return this.status === 'loading';
  }

  replace(value: T, empty = false) {
    this.value = value;
    this.status = empty ? 'empty' : 'ready';
    this.error = '';
  }

  markStale() {
    if (this.status === 'ready' || this.status === 'empty') {
      this.status = 'stale';
    }
    // Recorded regardless of status: replace()/patchLiveProgress() can flip
    // the status back to ready while a request is still pending, which would
    // otherwise lose the invalidation. The resolver forces status 'stale'
    // after applying the value so callers schedule a trailing reload.
    if (this.pending) {
      this.invalidatedWhilePending = true;
    }
  }

  fail(caught: unknown) {
    this.error = errorMessage(caught);
    this.status = 'failed';
  }

  async load(
    loader: () => Promise<T>,
    options: {
      force?: boolean;
      empty?: (value: T) => boolean;
      isCurrent?: () => boolean;
    } = {}
  ) {
    if (
      !options.force &&
      (this.status === 'ready' || this.status === 'empty')
    ) {
      return this.value;
    }
    // A forced reload orphans the in-flight request: its late completion is
    // discarded via the epoch check below instead of overwriting fresh data.
    if (this.pending && !options.force) return this.pending;
    this.epoch += 1;
    const epoch = this.epoch;
    this.invalidatedWhilePending = false;
    this.status =
      this.status === 'ready' ||
      this.status === 'empty' ||
      this.status === 'stale'
        ? 'stale'
        : 'loading';
    this.error = '';
    const request = loader()
      .then((value) => {
        if (epoch !== this.epoch) return this.value;
        if (options.isCurrent && !options.isCurrent()) return this.value;
        const wasInvalidated = this.invalidatedWhilePending;
        this.invalidatedWhilePending = false;
        this.replace(value, options.empty?.(value) ?? false);
        if (wasInvalidated) this.status = 'stale';
        return value;
      })
      .catch((caught) => {
        if (epoch !== this.epoch) throw caught;
        if (options.isCurrent && !options.isCurrent()) throw caught;
        this.fail(caught);
        throw caught;
      })
      .finally(() => {
        if (this.pending === request) this.pending = undefined;
      });
    this.pending = request;
    return request;
  }

  reset(value: T) {
    // Bumping the epoch discards any in-flight completion: its .then runs
    // but returns early instead of invoking replace().
    this.epoch += 1;
    this.pending = undefined;
    this.invalidatedWhilePending = false;
    this.value = value;
    this.status = 'idle';
    this.error = '';
  }
}
