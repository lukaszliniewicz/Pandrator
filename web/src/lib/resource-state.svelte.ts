import { errorMessage } from './errors';
import type { LoadState } from './api-models';

export class ResourceState<T> {
  value = $state<T>(undefined as T);
  status = $state<LoadState>('idle');
  error = $state('');
  private pending?: Promise<T>;

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
  }

  fail(caught: unknown) {
    this.error = errorMessage(caught);
    this.status = 'failed';
  }

  async load(
    loader: () => Promise<T>,
    options: { force?: boolean; empty?: (value: T) => boolean } = {}
  ) {
    if (
      !options.force &&
      (this.status === 'ready' || this.status === 'empty')
    ) {
      return this.value;
    }
    if (this.pending) return this.pending;
    this.status =
      this.status === 'ready' ||
      this.status === 'empty' ||
      this.status === 'stale'
        ? 'stale'
        : 'loading';
    this.error = '';
    const request = loader()
      .then((value) => {
        this.replace(value, options.empty?.(value) ?? false);
        return value;
      })
      .catch((caught) => {
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
    this.pending = undefined;
    this.value = value;
    this.status = 'idle';
    this.error = '';
  }
}
