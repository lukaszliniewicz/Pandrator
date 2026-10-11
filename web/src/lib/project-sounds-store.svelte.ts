import { projectSounds, type ProjectSoundCue } from './project-sounds-api';

/** Cues for mounted segment rows only; superseded requests cannot publish. */
export class ProjectSoundsStore {
  cues = $state<Record<string, ProjectSoundCue[]>>({});
  error = $state('');
  revision = $state(0);
  private ids: string[] = [];
  private timer: ReturnType<typeof setTimeout> | undefined;
  private epoch = 0;
  private disposed = false;
  private moreSegments = false;
  constructor(private sessionId: string) {}
  show(ids: string[]) {
    const distinct = [...new Set(ids)];
    this.moreSegments = distinct.length > 100;
    const next = distinct.slice(0, 100).sort();
    if (next.join(':') === this.ids.join(':')) return;
    this.ids = next;
    if (!next.length) {
      this.cues = {};
      this.error = '';
    }
    this.refresh();
  }
  refresh() {
    const epoch = ++this.epoch;
    clearTimeout(this.timer);
    this.timer = setTimeout(() => void this.load(epoch), 100);
  }
  private async load(epoch: number) {
    if (this.disposed || !this.ids.length) return;
    const ids = [...this.ids];
    try {
      const result = await projectSounds(this.sessionId, undefined, ids);
      if (this.disposed || epoch !== this.epoch) return;
      const cues: Record<string, ProjectSoundCue[]> = {};
      for (const cue of result.cues.items) {
        const id = cue.anchor.segment_id;
        if (id) (cues[id] ??= []).push(cue);
      }
      this.cues = cues;
      this.revision = result.revision;
      this.error =
        result.cues.next_cursor || this.moreSegments
          ? 'Open Sounds to see additional cues in these segments.'
          : '';
    } catch (error) {
      if (!this.disposed && epoch === this.epoch)
        this.error =
          error instanceof Error ? error.message : 'Could not load sounds.';
    }
  }
  dispose() {
    this.disposed = true;
    ++this.epoch;
    clearTimeout(this.timer);
  }
}
