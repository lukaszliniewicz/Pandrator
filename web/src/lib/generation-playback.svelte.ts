import type { GenerationSegment } from './api-models';
import type { PlayableTake } from './generation-view-models';

export type PlaybackItem = Pick<
  GenerationSegment,
  'id' | 'removed' | 'silence_after_ms' | 'assembly_silence_after_ms'
>;

type GenerationPlaybackOptions<T extends PlaybackItem> = {
  getItems: () => T[];
  getNextCursor: () => number | string | null | undefined;
  loadMore: () => Promise<void>;
  getTake: (
    item: T
  ) => Pick<PlayableTake, 'artifact_id' | 'assembly_silences'> | undefined;
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
  private gapActive = false;

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
    this.gapActive = false;
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
    } else if (!this.gapActive) {
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
    let pendingSilence = 0;
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
        if (item.removed || !this.options.getTake(item)?.artifact_id) continue;
        if (pendingSilence > 0) {
          await this.waitForSilence(pendingSilence, token);
        }
        while (this.paused && token === this.token) {
          await new Promise((resolve) => window.setTimeout(resolve, 50));
        }
        if (token !== this.token) break;
        pendingSilence = 0;
        const completed = await this.playTake(item, token, true);
        if (completed) {
          pendingSilence = Number(
            item.assembly_silence_after_ms ?? item.silence_after_ms ?? 0
          );
        }
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

  private async playTake(
    item: T,
    token: number,
    includeInternalSilences = false
  ) {
    const take = this.options.getTake(item);
    if (!take?.artifact_id || item.removed || token !== this.token)
      return false;

    this.activePlayingId = item.id;
    this.options.onSelect(item.id);
    const audio = new Audio(`/api/v1/artifacts/${take.artifact_id}/content`);
    const pauses = includeInternalSilences
      ? (take.assembly_silences ?? []).filter(
          (gap) =>
            Number.isFinite(gap.at_ms) &&
            gap.at_ms > 0 &&
            Number.isFinite(gap.duration_ms) &&
            gap.duration_ms > 0
        )
      : [];
    let pauseIndex = 0;
    let waiting = false;
    let settled = false;
    let timer: number | undefined;
    const completed = await new Promise<boolean>((resolve) => {
      const finish = (played: boolean) => {
        settled = true;
        if (timer !== undefined) window.clearTimeout(timer);
        audio.ontimeupdate = null;
        audio.onplay = null;
        resolve(played);
      };
      const checkGap = () => {
        if (settled || waiting || token !== this.token) return;
        if (timer !== undefined) window.clearTimeout(timer);
        const gap = pauses[pauseIndex];
        if (!gap || this.paused) return;
        const until = gap.at_ms - audio.currentTime * 1000;
        if (until > 1) {
          timer = window.setTimeout(checkGap, Math.max(4, Math.min(until, 50)));
          return;
        }
        pauseIndex += 1;
        waiting = true;
        this.gapActive = true;
        audio.pause();
        void this.waitForSilence(gap.duration_ms, token).then(async () => {
          while (this.paused && !settled && token === this.token) {
            await new Promise((resume) => window.setTimeout(resume, 50));
          }
          if (settled || token !== this.token || this.audio !== audio) return;
          this.gapActive = false;
          waiting = false;
          void audio
            .play()
            .then(checkGap)
            .catch(() => finish(false));
        });
      };
      this.resolveAudio = () => finish(false);
      this.audio = audio;
      audio.onended = () => finish(true);
      audio.onerror = () => finish(false);
      audio.ontimeupdate = checkGap;
      audio.onplay = checkGap;
      void audio
        .play()
        .then(checkGap)
        .catch(() => finish(false));
    });
    // stop() settles the previous attempt before its continuation runs. A
    // replacement may already own these handles by then; keep it stoppable.
    if (this.audio === audio) {
      this.resolveAudio = null;
      this.audio = null;
    }

    return completed && token === this.token;
  }
}
