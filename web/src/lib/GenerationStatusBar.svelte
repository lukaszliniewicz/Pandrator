<script lang="ts">
  import { onMount, untrack } from 'svelte';
  import { Pause, Play, Square } from '@lucide/svelte';
  import { appState } from './app-state.svelte';
  import { generationApi } from './domain-api';
  import { errorMessage } from './errors';
  import { useGenerationWorkspace } from './generation-workspace-state.svelte';

  let {
    sessionId,
    workspaceVisible
  }: { sessionId: string; workspaceVisible: boolean } = $props();
  const workspaceState = useGenerationWorkspace(untrack(() => sessionId));
  const store = workspaceState.summary;
  const summary = $derived(store.value);
  const run = $derived(summary?.active_run);
  const active = $derived(
    [
      'queued',
      'running',
      'pausing',
      'pause_requested',
      'cancel_requested'
    ].includes(run?.status ?? '') ||
      ['queued', 'running'].includes(summary?.assembly?.status ?? '')
  );
  const activity = $derived(run ?? summary?.assembly);
  const percent = $derived(
    Math.max(
      0,
      Math.min(100, Math.round(Number(activity?.progress ?? 0) * 100))
    )
  );
  let busy = $state(false);
  let error = $state('');

  async function action(kind: 'pause' | 'resume' | 'cancel') {
    if (!run || busy) return;
    busy = true;
    error = '';
    try {
      await generationApi.runAction(run.id, kind);
      await store.load(true);
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      busy = false;
    }
  }

  $effect(() => {
    if (!workspaceVisible && !workspaceState.playback.active)
      untrack(() => workspaceState.releaseQueue());
  });

  onMount(() => {
    const disconnect = store.connect();
    // The summary is the only generation read retained on other project tabs.
    const interval = window.setInterval(
      () => {
        if (!active || document.visibilityState === 'hidden') return;
        void store.load(true).catch(() => undefined);
      },
      appState.eventsHealthy ? 10_000 : 2_500
    );
    const visible = () => {
      if (document.visibilityState === 'visible')
        void store.load(true).catch(() => undefined);
    };
    document.addEventListener('visibilitychange', visible);
    return () => {
      disconnect();
      window.clearInterval(interval);
      document.removeEventListener('visibilitychange', visible);
    };
  });
</script>

{#if !workspaceVisible && (summary?.plan_revision_id || active || workspaceState.playback.active || store.status === 'failed')}
  <div class="h-24" aria-hidden="true"></div>
  <section
    class="surface generation-status fixed inset-x-3 bottom-3 z-40 flex flex-wrap items-center gap-3 rounded-2xl border border-[var(--line)] px-4 py-3 text-sm shadow-lg md:left-[calc(var(--sidebar-offset,5rem)+.35rem)] md:right-[.35rem]"
    aria-label="Generation status"
    data-testid="generation-status-bar"
  >
    <a
      class="font-semibold text-[var(--accent)]"
      href={`/sessions/${sessionId}/generation`}>Open generation</a
    >
    <span class="muted text-xs"
      >{summary?.total ?? 0} segments{run
        ? ` · ${run.status.replaceAll('_', ' ')}`
        : ''}</span
    >
    {#if active}
      <progress
        class="w-28 accent-[var(--accent)]"
        max="100"
        value={percent}
        aria-label="Generation progress"
      ></progress>
      <span
        class="muted min-w-0 flex-1 truncate text-xs"
        title={activity?.progress_detail ?? ''}
        >{activity?.progress_detail ?? `${percent}%`}</span
      >
    {/if}
    {#if run && ['queued', 'running'].includes(run.status)}
      <button
        class="btn btn-secondary"
        disabled={busy}
        onclick={() => action('pause')}
        aria-label="Pause generation"><Pause size={15} /></button
      >
      <button
        class="btn btn-secondary"
        disabled={busy}
        onclick={() => action('cancel')}
        aria-label="Cancel generation"><Square size={15} /></button
      >
    {:else if run?.status === 'paused'}
      <button
        class="btn btn-secondary"
        disabled={busy}
        onclick={() => action('resume')}
        ><Play size={15} /> Resume generation</button
      >
    {/if}
    {#if workspaceState.playback.active}
      <span class="muted text-xs">Playing segments</span>
      <button
        class="btn btn-secondary"
        onclick={() => workspaceState.playback.togglePause()}
        aria-label={workspaceState.playback.paused
          ? 'Resume playback'
          : 'Pause playback'}
        >{#if workspaceState.playback.paused}<Play size={15} />{:else}<Pause
            size={15}
          />{/if}</button
      >
      <button
        class="btn btn-secondary"
        onclick={() => workspaceState.playback.stop()}
        aria-label="Stop playback"><Square size={15} /></button
      >
    {/if}
    {#if error || workspaceState.error || store.status === 'failed'}
      <p class="w-full text-xs text-red-500" role="alert">
        {error || workspaceState.error || store.error}
      </p>
      {#if store.status === 'failed'}<button
          class="btn btn-secondary"
          onclick={() => store.load(true).catch(() => undefined)}
          >Retry status</button
        >{/if}
    {/if}
  </section>
{/if}
