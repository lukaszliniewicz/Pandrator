import type { GenerationSegment } from './api-models';
import type { PlayableTake } from './generation-view-models';

export type PlaybackItem = Pick<
  GenerationSegment,
  'id' | 'removed' | 'silence_after_ms'
>;

type GenerationPlaybackOptions<T extends PlaybackItem> = {
  getItems: () => T[];
  getNextCursor: () => number | string | null | undefined;
  loadMore: () => Promise<void>;
  getTake: (item: T) => Pick<PlayableTake, 'artifact_id'> | undefined;
  onSelect: (id: string) => void;
  onError: (message: string) => void;
};

/**
 * Owns the browser Audio object and cancellation token for sequential segment
 * playback. Keeping this lifecycle outside the drawer makes teardown explicit
 * and leaves the component responsible only for selection and presentation.
 */
export class GenerationPlaybackController<
  T extends PlaybackItem = GenerationSegment
> {
  active = $state(false);
  paused = $state(false);
  activePlayingId = $state('');

  private audio: HTMLAudioElement | null = null;
  private resolveAudio: (() => void) | null = null;
  private token = 0;

  constructor(private readonly options: GenerationPlaybackOptions<T>) {}

  stop() {
    this.token += 1;
    this.audio?.pause();
    this.audio = null;
    this.resolveAudio?.();
    this.resolveAudio = null;
    this.active = false;
    this.paused = false;
    this.activePlayingId = '';
  }

  toggle(startId: string) {
    if (!this.active) {
      void this.playFrom(startId);
      return;
    }
    this.togglePause();
  }

  togglePause() {
    if (!this.active) return;
    this.paused = !this.paused;
    if (this.paused) {
      this.audio?.pause();
    } else {
      void this.audio?.play().catch(() => {
        this.options.onError('Playback could not be resumed.');
      });
    }
  }

  async playOnly(item: T) {
    this.stop();
    const token = this.token;
    this.active = true;
    await this.playTake(item, token);
    if (token === this.token) this.stop();
  }

  async playFrom(startId: string) {
    this.stop();
    const token = this.token;
    this.active = true;
    let index = Math.max(
      0,
      this.options.getItems().findIndex((item) => item.id === startId)
    );

    let previousId = '';
    while (token === this.token) {
      const items = this.options.getItems();
      if (previousId) {
        const previousIndex = items.findIndex((item) => item.id === previousId);
        if (previousIndex >= 0) index = previousIndex + 1;
      }
      if (index >= items.length) {
        if (this.options.getNextCursor() == null) break;
        const previousLength = items.length;
        await this.options.loadMore();
        if (this.options.getItems().length === previousLength) break;
        continue;
      }
      const item = items[index++];
      if (item) {
        previousId = item.id;
        await this.playTake(item, token);
      }
    }

    if (token === this.token) this.stop();
  }

  private async waitForSilence(milliseconds: number, token: number) {
    let remaining = Math.max(0, milliseconds);
    let previous = performance.now();
    while (remaining > 0 && token === this.token) {
      await new Promise((resolve) =>
        window.setTimeout(resolve, Math.min(remaining, 50))
      );
      const now = performance.now();
      if (!this.paused) remaining -= now - previous;
      previous = now;
    }
  }

  private async playTake(item: T, token: number) {
    const take = this.options.getTake(item);
    if (!take || item.removed) return;

    this.activePlayingId = item.id;
    this.options.onSelect(item.id);
    const audio = new Audio(`/api/v1/artifacts/${take.artifact_id}/content`);
    await new Promise<void>((resolve) => {
      this.resolveAudio = resolve;
      this.audio = audio;
      audio.onended = () => resolve();
      audio.onerror = () => resolve();
      void audio.play().catch(resolve);
    });
    // stop() settles the previous attempt before its continuation runs. A
    // replacement may already own these handles by then; keep it stoppable.
    if (this.audio === audio) {
      this.resolveAudio = null;
      this.audio = null;
    }

    if (token === this.token) {
      await this.waitForSilence(Number(item.silence_after_ms || 0), token);
    }
  }
}
