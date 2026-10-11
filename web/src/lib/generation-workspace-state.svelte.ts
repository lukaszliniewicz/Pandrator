import { getContext } from 'svelte';
import type { GenerationSegmentPage } from './api-models';
import type { SegmentFilter } from './generation-store.svelte';
import {
  GenerationPlaybackController,
  type PlaybackItem
} from './generation-playback.svelte';
import { generationApi } from './domain-api';
import { errorMessage } from './errors';
import {
  selectedGenerationTake,
  type TakeSelection
} from './generation-take-selection';
import { GenerationSummaryStore } from './generation-summary-store.svelte';

export const GENERATION_WORKSPACES = Symbol('generation-workspaces');

export type GenerationViewPreferences = {
  selectedRunId: string;
  selectedRunVersionId: string;
  audioVersionSide: 'before' | 'after';
  settingsSourceRunId: string;
  filter: SegmentFilter;
  selectedRow: string;
  selectedRows: string[];
  viewMode: 'segments' | 'reading';
  showAudioColumns?: boolean;
  showDirectionDetails?: boolean;
  textMode: 'display' | 'speech';
  searchQuery: string;
  searchOptions: { matchCase: boolean; wholeWord: boolean };
  searchOpen: boolean;
  searchScope: 'cue' | 'tts';
  scrollTop: number;
  startOrdinal: number;
  anchorId: string;
};

type QueuedAudio = PlaybackItem & { artifact_id?: string };

/** Project lifetime owns only view preferences and the compact playback queue. */
export class GenerationWorkspaceState {
  preferences: GenerationViewPreferences | null = null;
  error = $state('');
  items: QueuedAudio[] = [];
  loadFromView: (() => Promise<void>) | null = null;
  private query: URLSearchParams | null = null;
  private scope = '';
  private selection: TakeSelection = {
    historical: false,
    audioVersion: false,
    sequence: 0,
    sequences: {}
  };
  private controller?: AbortController;
  readonly summary: GenerationSummaryStore;
  readonly playback = new GenerationPlaybackController<QueuedAudio>({
    getItems: () => this.items,
    getNextCursor: () => this.query?.get('cursor'),
    loadMore: () => this.loadMore(),
    getTake: (item) =>
      item.artifact_id ? { artifact_id: item.artifact_id } : undefined,
    onSelect: () => {},
    onError: (message) => {
      this.error = message;
    }
  });

  constructor(readonly sessionId: string) {
    this.summary = new GenerationSummaryStore(sessionId);
  }

  sync(
    page: GenerationSegmentPage,
    query: URLSearchParams,
    selection: TakeSelection
  ) {
    const scopeQuery = new URLSearchParams(query);
    scopeQuery.delete('cursor');
    const scope = scopeQuery.toString();
    if (this.scope && this.scope !== scope) this.playback.stop();
    this.scope = scope;
    this.selection = selection;
    this.items = page.items.map((item) => ({
      id: item.id,
      removed: item.removed,
      silence_after_ms: item.silence_after_ms,
      artifact_id: selectedGenerationTake(item, selection)?.artifact_id
    }));
    this.query = page.next_cursor == null ? null : new URLSearchParams(query);
    this.query?.set('cursor', String(page.next_cursor));
  }

  private async loadMore() {
    if (this.loadFromView) return this.loadFromView();
    if (!this.query) return;
    const query = new URLSearchParams(this.query);
    query.set(
      'fields',
      'id,ordinal,removed,silence_after_ms,takes,history_take_id'
    );
    const scope = this.scope;
    this.controller?.abort();
    const controller = new AbortController();
    this.controller = controller;
    try {
      const page = await generationApi.segments(
        this.sessionId,
        query,
        controller.signal
      );
      if (controller.signal.aborted || scope !== this.scope) return;
      const known = new Set(this.items.map((item) => item.id));
      this.items.push(
        ...page.items
          .filter((item) => !known.has(item.id))
          .map((item) => ({
            id: item.id,
            removed: item.removed,
            silence_after_ms: item.silence_after_ms,
            artifact_id: selectedGenerationTake(item, this.selection)
              ?.artifact_id
          }))
      );
      if (
        page.next_cursor === query.get('cursor') ||
        String(page.next_cursor) === query.get('cursor')
      )
        throw new Error('Segment playback could not advance to the next page.');
      this.query = page.next_cursor == null ? null : new URLSearchParams(query);
      this.query?.set('cursor', String(page.next_cursor));
    } catch (caught) {
      if (!controller.signal.aborted) {
        this.error = errorMessage(caught);
        this.query = null;
      }
    } finally {
      if (this.controller === controller) this.controller = undefined;
    }
  }

  dispose() {
    this.controller?.abort();
    this.playback.stop();
    this.loadFromView = null;
    this.items = [];
    this.query = null;
  }

  releaseQueue() {
    this.controller?.abort();
    this.items = [];
    this.query = null;
    this.scope = '';
  }
}

export function useGenerationWorkspace(sessionId: string) {
  const states = getContext<Map<string, GenerationWorkspaceState>>(
    GENERATION_WORKSPACES
  );
  let state = states.get(sessionId);
  if (!state) {
    state = new GenerationWorkspaceState(sessionId);
    states.set(sessionId, state);
  }
  return state;
}
