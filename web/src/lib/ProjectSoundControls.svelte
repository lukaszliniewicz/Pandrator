<script lang="ts">
  import { Bell, X, Pencil, Trash2 } from '@lucide/svelte';
  import { onDestroy, untrack } from 'svelte';
  import { apiJson, ApiMutationAttempt } from './api';
  import { errorMessage } from './errors';
  import { modalFocus } from './modal-focus';
  import { ProjectSoundsStore } from './project-sounds-store.svelte';
  import {
    projectSounds,
    projectSoundMix,
    removeProjectSound,
    restoreProjectSounds,
    runProjectSounds,
    soundPlacementLabel,
    type ProjectSoundPlan,
    type ProjectSoundCue,
    type SoundAnchor,
    type SoundMixStatus
  } from './project-sounds-api';
  import { soundAnchorForSelection } from './sound-selection';
  import type { SpeechSelection } from './speech-annotations';
  import type { GenerationSegment } from './api-models';
  import { soundAudioUrl, cancelSound } from './sound-api';
  import SoundCuePicker from './SoundCuePicker.svelte';
  import AudioPlayer from './AudioPlayer.svelte';
  let {
    sessionId,
    workflowKind,
    editable,
    baseArtifactId,
    onmixchange
  }: {
    sessionId: string;
    workflowKind: string;
    editable: boolean;
    baseArtifactId?: string | null;
    onmixchange?: (status: SoundMixStatus | null) => void;
  } = $props();
  export const store = new ProjectSoundsStore(untrack(() => sessionId));
  let opened = $state(false),
    loading = $state(false),
    busy = $state(false),
    error = $state('');
  let plan = $state<ProjectSoundPlan | null>(null),
    mix = $state<SoundMixStatus | null>(null);
  let target = $state<{
    anchor: SoundAnchor;
    cue?: ProjectSoundCue;
    itemId?: string;
  } | null>(null);
  let jobId = $state(''),
    jobStatus = $state(''),
    restoreId = $state('');
  let previewMix = $state(false);
  let alive = true,
    serial = 0,
    mixSerial = 0,
    timer: ReturnType<typeof setTimeout> | undefined;
  const runAttempt = new ApiMutationAttempt();
  export function addSelection(selection: SpeechSelection) {
    if (!editable) return;
    try {
      target = { anchor: soundAnchorForSelection(selection) };
    } catch (failure) {
      error = errorMessage(failure);
      opened = true;
    }
  }
  export function addSegment(item: GenerationSegment) {
    if (editable)
      target = { anchor: { kind: 'after_segment', segment_id: item.id } };
  }
  export function editCue(cue: ProjectSoundCue) {
    if (editable) target = { anchor: cue.anchor, cue };
  }
  export function useSaved(anchor: SoundAnchor, itemId: string) {
    if (editable) target = { anchor, itemId };
  }
  export function refresh() {
    store.refresh();
    if (opened) void load();
    else void loadMix();
  }
  function acceptMix(next: SoundMixStatus | null) {
    mix = next;
    previewMix = false;
    onmixchange?.(next);
  }
  async function loadMix(scope = sessionId, base = baseArtifactId) {
    const request = ++mixSerial;
    try {
      const next = await projectSoundMix(scope, base ?? undefined);
      if (
        alive &&
        request === mixSerial &&
        scope === sessionId &&
        base === baseArtifactId
      )
        acceptMix(next);
    } catch {
      if (
        alive &&
        request === mixSerial &&
        scope === sessionId &&
        base === baseArtifactId
      )
        acceptMix(null);
    }
  }
  $effect(() => {
    const scope = sessionId;
    const base = baseArtifactId;
    untrack(() => {
      acceptMix(null);
      void loadMix(scope, base);
    });
  });
  async function load(cursor?: string) {
    const request = ++serial;
    loading = true;
    error = '';
    try {
      const [next, status] = await Promise.all([
        projectSounds(sessionId, cursor),
        projectSoundMix(sessionId, baseArtifactId ?? undefined)
      ]);
      if (!alive || request !== serial) return;
      plan = next;
      acceptMix(status);
    } catch (failure) {
      if (alive && request === serial) error = errorMessage(failure);
    } finally {
      if (alive && request === serial) loading = false;
    }
  }
  async function poll(id: string) {
    try {
      const job = await apiJson<{
        status: string;
        error_message?: string | null;
      }>(`/jobs/${encodeURIComponent(id)}`);
      if (!alive || jobId !== id) return;
      jobStatus = job.status;
      if (
        ['queued', 'running', 'retrying', 'cancel_requested'].includes(
          job.status
        )
      )
        timer = setTimeout(() => void poll(id), 2000);
      else {
        busy = false;
        jobId = '';
        if (job.status === 'failed')
          error = job.error_message ?? 'Sound operation failed.';
        refresh();
      }
    } catch (failure) {
      if (alive && jobId === id) {
        error = errorMessage(failure);
        busy = false;
      }
    }
  }
  async function run(action: 'render' | 'resolve') {
    if (!plan || busy) return;
    busy = true;
    error = '';
    try {
      const receipt = await runProjectSounds(
        sessionId,
        plan.revision,
        action,
        baseArtifactId ?? undefined,
        runAttempt
      );
      if (!alive) return;
      if (receipt.job_id && ['queued', 'running'].includes(receipt.status)) {
        jobId = receipt.job_id;
        jobStatus = receipt.status;
        void poll(jobId);
      } else {
        busy = false;
        refresh();
      }
    } catch (failure) {
      if (alive) {
        error = errorMessage(failure);
        busy = false;
      }
    }
  }
  async function remove(cue: ProjectSoundCue) {
    if (!plan || busy) return;
    busy = true;
    try {
      await removeProjectSound(sessionId, plan.revision, cue.logical_id);
      if (alive) refresh();
    } catch (failure) {
      if (alive) error = errorMessage(failure);
    } finally {
      if (alive) busy = false;
    }
  }
  async function restore() {
    if (!plan || !restoreId.trim() || busy) return;
    busy = true;
    try {
      await restoreProjectSounds(sessionId, plan.revision, restoreId.trim());
      if (alive) refresh();
    } catch (failure) {
      if (alive) error = errorMessage(failure);
    } finally {
      if (alive) busy = false;
    }
  }
  onDestroy(() => {
    alive = false;
    ++serial;
    ++mixSerial;
    clearTimeout(timer);
    store.dispose();
  });
</script>

<button
  class="btn btn-secondary"
  aria-label="Project sounds and music"
  onclick={() => {
    opened = true;
    void load();
  }}><Bell size={14} />Sounds & Music</button
>
{#if opened}<div
    class="fixed inset-0 z-[95] grid place-items-center bg-black/45 p-3"
    role="presentation"
    onclick={(event) => {
      if (event.target === event.currentTarget) opened = false;
    }}
  >
    <div
      use:modalFocus={{ onclose: () => (opened = false) }}
      role="dialog"
      aria-modal="true"
      aria-labelledby="project-sounds-title"
      tabindex="-1"
      class="max-h-[90vh] w-full max-w-2xl overflow-auto rounded-2xl border border-[var(--line)] bg-[var(--paper)] p-5 shadow-xl"
    >
      <header class="flex items-center justify-between">
        <h3 id="project-sounds-title" class="text-lg font-semibold">
          Project sounds & music
        </h3>
        <button
          class="btn btn-secondary"
          aria-label="Close project sounds"
          onclick={() => (opened = false)}><X size={16} /></button
        >
      </header>
      <p class="muted my-3 text-sm">
        {workflowKind === 'subtitles'
          ? 'Sounds are previewed separately. Subtitle wording and timing stay intact.'
          : 'Saved sounds are independent of the narrator and speech model. Mix changes reuse existing speech.'}
      </p>
      {#if loading}<p class="muted" role="status">Loading…</p>{/if}
      {#if plan}<p class="muted mb-3 text-xs">
          Sound plan v{plan.revision} · {plan.plan_id ?? 'No sounds added'}
        </p>
        {#each plan.cues.items as cue (cue.logical_id)}<div
            class="mb-2 flex items-center gap-3 rounded-lg border border-[var(--line)] p-3"
          >
            <div class="min-w-0 flex-1">
              <strong class="block truncate text-sm">{cue.sound.name}</strong>
              <p class="muted text-xs">
                {soundPlacementLabel(cue.anchor)} · {cue.playback
                  .mode}{!cue.included ? ' · Excluded' : ''} · {cue.placement_status ===
                'ready'
                  ? 'Ready'
                  : 'Needs placement'}
              </p>
              {#if cue.anchor.resolution?.reason}<p class="muted text-xs">
                  {cue.anchor.resolution.reason}
                </p>{/if}
            </div>
            {#if editable}<button
                class="btn btn-secondary"
                aria-label={`Edit ${cue.sound.name}`}
                disabled={busy}
                onclick={() => editCue(cue)}><Pencil size={14} /></button
              ><button
                class="btn btn-secondary"
                aria-label={`Remove ${cue.sound.name}`}
                disabled={busy}
                onclick={() => void remove(cue)}><Trash2 size={14} /></button
              >{/if}
          </div>{/each}
        {#if !plan.cues.items.length}<p class="muted my-4 text-sm">
            {editable
              ? 'Choose Sound effect in a segment or its selection toolbar to add a saved sound.'
              : 'No effects on this page.'}
          </p>{/if}
        {#if plan.cues.next_cursor}<button
            class="btn btn-secondary"
            disabled={busy || loading}
            onclick={() => void load(plan!.cues.next_cursor!)}>Next cues</button
          >{/if}
      {/if}
      <div class="mt-4 flex flex-wrap gap-2">
        {#if editable && workflowKind !== 'subtitles'}<button
            class="btn btn-secondary"
            disabled={busy || !baseArtifactId || !plan?.revision}
            onclick={() => void run('render')}>Render sound mix</button
          ><button
            class="btn btn-secondary"
            disabled={busy || !plan?.revision}
            onclick={() => void run('resolve')}>Resolve word placements</button
          >{/if}
        <a class="btn btn-secondary" href="/sounds">Sounds & Music library</a>
        {#if jobId}<button
            class="btn btn-secondary"
            onclick={() => void cancelSound(jobId)}>Cancel {jobStatus}</button
          >{/if}
      </div>
      {#if !baseArtifactId && workflowKind !== 'subtitles'}<p
          class="muted mt-2 text-xs"
        >
          Assemble the speech audio before rendering a sound mix.
        </p>{/if}
      {#if mix?.artifact_id && ['ready', 'completed'].includes(mix.status)}<div
          class="mt-4"
        >
          {#if previewMix}<AudioPlayer
              src={soundAudioUrl(mix.artifact_id)}
              label="Speech with sound effects"
              preload="none"
            />{:else}<button
              class="btn btn-secondary"
              onclick={() => (previewMix = true)}>Preview sound mix</button
            >{/if}
        </div>{:else if mix}<p class="muted mt-3 text-xs">
          Mix: {mix.status}
        </p>{/if}
      {#if editable && plan?.revision}<details class="mt-4 text-sm">
          <summary class="cursor-pointer">Restore a previous sound plan</summary
          ><label class="mt-2 block"
            >Plan ID<input
              class="input mt-1 w-full"
              bind:value={restoreId}
              maxlength="36"
            /></label
          ><button
            class="btn btn-secondary mt-2"
            disabled={busy || !restoreId.trim()}
            onclick={() => void restore()}>Restore as a new version</button
          >
        </details>{/if}
      {#if store.error}<p class="muted mt-3 text-xs" role="status">
          {store.error}
        </p>{/if}
      {#if error}<p class="mt-3 text-sm text-red-700" role="alert">
          {error}
        </p>{/if}
    </div>
  </div>{/if}
{#if target}{#key target}<SoundCuePicker
      {sessionId}
      {workflowKind}
      {...target}
      onclose={() => (target = null)}
      onchanged={refresh}
    />{/key}{/if}
