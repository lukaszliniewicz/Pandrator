<script lang="ts">
  import { page } from '$app/state';
  import {
    Bot,
    Check,
    ChevronRight,
    CircleAlert,
    Clock3,
    Download,
    LoaderCircle,
    Play,
    Plus,
    RefreshCw,
    Save,
    Scissors,
    Search,
    Settings2,
    Sparkles,
    X,
    Trash2
  } from '@lucide/svelte';
  import { onDestroy, onMount } from 'svelte';
  import {
    artifactApi,
    jobApi,
    mediaEditApi,
    sessionApi
  } from '$lib/domain-api';
  import type {
    JobRecord,
    MediaEditPlan,
    MediaEditRange,
    MediaEditState
  } from '$lib/api-models';
  import { errorMessage } from '$lib/errors';
  import MediaTimeline from '$lib/MediaTimeline.svelte';
  import { modalFocus } from '$lib/modal-focus';

  type CutRange = MediaEditRange;

  const sessionId = String(page.params.id);
  let workspaceState = $state<MediaEditState | null>(null);
  let plan = $state<MediaEditPlan | null>(null);
  let cuts = $state<CutRange[]>([]);
  let peaks = $state<number[]>([]);
  let detailPeaks = $state<number[]>([]);
  let detailPeaksStartMs = $state(0);
  let detailPeaksEndMs = $state(0);
  let video = $state<HTMLVideoElement>();
  let currentMs = $state(0);
  let cutStartMs = $state<number | null>(null);
  let cutEndMs = $state<number | null>(null);
  let instructions = $state(
    'Remove setup chatter, screen-sharing logistics, breaks, repeated takes, and private discussion. Keep the complete public presentation and its natural opening and closing.'
  );
  let search = $state('');
  let busy = $state('');
  let message = $state('');
  let error = $state('');
  let activeJob = $state<JobRecord | null>(null);
  let pollTimer: number | undefined;
  let detailTimer: number | undefined;
  let playbackFrame: number | undefined;
  let detailRequest: AbortController | undefined;
  let detailKey = '';
  let pendingDetailKey = $state('');
  let loopEndMs: number | null = null;
  let captionTrackUrl = $state('');
  let cutSequence = 0;
  let proposalOpen = $state(false);
  let proposalModel = $state('default');
  let proposalModels = $state<
    { value: string; label: string; isDefault: boolean }[]
  >([]);
  let proposalModelsLoading = $state(false);
  let proposalModelsError = $state('');

  const activeCue = $derived(
    plan?.cues.find(
      (cue) => currentMs >= cue.start_ms && currentMs < cue.end_ms
    ) ?? null
  );
  const displayedCues = $derived.by(() => {
    const query = search.trim().toLocaleLowerCase();
    if (query)
      return (plan?.cues ?? []).filter((cue) =>
        `${cue.speaker ?? ''} ${cue.text}`.toLocaleLowerCase().includes(query)
      );
    if (!activeCue) return (plan?.cues ?? []).slice(0, 500);
    const index = plan?.cues.findIndex((cue) => cue.id === activeCue.id) ?? 0;
    return (plan?.cues ?? []).slice(Math.max(0, index - 120), index + 380);
  });
  const keptDurationMs = $derived(
    plan
      ? keepFromCuts(cuts, plan.duration_ms).reduce(
          (total, range) => total + range.end_ms - range.start_ms,
          0
        )
      : 0
  );

  function formatTime(value: number) {
    const milliseconds = Math.max(0, Math.round(value));
    const seconds = Math.floor(milliseconds / 1000);
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const remainder = seconds % 60;
    const fraction = milliseconds % 1000;
    return `${hours ? `${hours}:` : ''}${String(minutes).padStart(hours ? 2 : 1, '0')}:${String(remainder).padStart(2, '0')}.${String(fraction).padStart(3, '0')}`;
  }

  function vttTime(value: number) {
    const milliseconds = Math.max(0, Math.round(value));
    const hours = Math.floor(milliseconds / 3_600_000);
    const minutes = Math.floor((milliseconds % 3_600_000) / 60_000);
    const seconds = Math.floor((milliseconds % 60_000) / 1000);
    const fraction = milliseconds % 1000;
    return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}.${String(fraction).padStart(3, '0')}`;
  }

  function escapeVttText(value: string) {
    return value
      .replaceAll('&', '&amp;')
      .replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;')
      .replaceAll('-->', '→');
  }

  function escapeVttVoice(value: string) {
    return value.replaceAll('&', '&amp;').replaceAll('>', '&gt;');
  }

  function updateCaptionTrack(next: MediaEditPlan | null) {
    if (captionTrackUrl) URL.revokeObjectURL(captionTrackUrl);
    captionTrackUrl = '';
    if (!next?.cues.length) return;
    const body = next.cues
      .map((cue) => {
        const text = escapeVttText(cue.text);
        const caption = cue.speaker
          ? `<v ${escapeVttVoice(cue.speaker)}>${text}</v>`
          : text;
        return `${vttTime(cue.start_ms)} --> ${vttTime(cue.end_ms)}\n${caption}`;
      })
      .join('\n\n');
    captionTrackUrl = URL.createObjectURL(
      new Blob([`WEBVTT\n\n${body}\n`], { type: 'text/vtt' })
    );
  }

  function proposalCuts(next: MediaEditPlan | null) {
    const proposal = next?.evidence.agent_proposal;
    if (!proposal || typeof proposal !== 'object') return [];
    const records = (proposal as { cuts?: unknown }).cuts;
    if (!Array.isArray(records)) return [];
    return records.filter(
      (item): item is { start_ms: number; end_ms: number; reason: string } =>
        Boolean(
          item &&
          typeof item === 'object' &&
          typeof item.start_ms === 'number' &&
          typeof item.end_ms === 'number' &&
          typeof item.reason === 'string'
        )
    );
  }

  function cutsFromKeep(
    ranges: MediaEditRange[],
    durationMs: number,
    next: MediaEditPlan | null = null
  ) {
    const result: CutRange[] = [];
    let cursor = 0;
    for (const range of ranges
      .slice()
      .sort((a, b) => a.start_ms - b.start_ms)) {
      if (range.start_ms > cursor)
        result.push({
          id: `cut-${result.length + 1}`,
          start_ms: cursor,
          end_ms: range.start_ms,
          label: 'Removed section'
        });
      cursor = Math.max(cursor, range.end_ms);
    }
    if (cursor < durationMs)
      result.push({
        id: `cut-${result.length + 1}`,
        start_ms: cursor,
        end_ms: durationMs,
        label: 'Removed section'
      });
    const proposed = proposalCuts(next);
    return result.map((range) => {
      const reasons = proposed
        .filter(
          (item) => item.start_ms < range.end_ms && item.end_ms > range.start_ms
        )
        .map((item) => item.reason)
        .filter((reason, index, values) => values.indexOf(reason) === index);
      return reasons.length ? { ...range, label: reasons.join(' / ') } : range;
    });
  }

  function keepFromCuts(values: CutRange[], durationMs: number) {
    const normalized = values
      .map((range) => ({
        ...range,
        start_ms: Math.max(0, Math.min(durationMs, Math.round(range.start_ms))),
        end_ms: Math.max(0, Math.min(durationMs, Math.round(range.end_ms)))
      }))
      .filter((range) => range.end_ms > range.start_ms)
      .sort((a, b) => a.start_ms - b.start_ms);
    const merged: CutRange[] = [];
    for (const range of normalized) {
      const previous = merged.at(-1);
      if (previous && range.start_ms <= previous.end_ms) {
        previous.end_ms = Math.max(previous.end_ms, range.end_ms);
      } else merged.push({ ...range });
    }
    const keep: MediaEditRange[] = [];
    let cursor = 0;
    for (const range of merged) {
      if (range.start_ms > cursor)
        keep.push({
          id: `keep-${keep.length + 1}`,
          start_ms: cursor,
          end_ms: range.start_ms,
          label: 'Retained'
        });
      cursor = Math.max(cursor, range.end_ms);
    }
    if (cursor < durationMs)
      keep.push({
        id: `keep-${keep.length + 1}`,
        start_ms: cursor,
        end_ms: durationMs,
        label: 'Retained'
      });
    return keep;
  }

  function adopt(next: MediaEditState) {
    workspaceState = next;
    plan = next.plan ?? null;
    updateCaptionTrack(plan);
    if (plan) {
      cuts = cutsFromKeep(plan.keep_ranges, plan.duration_ms, plan);
      instructions = plan.instructions || instructions;
    } else cuts = [];
  }

  async function load() {
    error = '';
    try {
      adopt(await mediaEditApi.state(sessionId));
      if (workspaceState?.readiness.source_media_artifact) void loadWaveform();
    } catch (caught) {
      error = errorMessage(caught);
    }
  }

  async function loadWaveform() {
    const artifactId =
      plan?.source_media_artifact.id ??
      workspaceState?.readiness.source_media_artifact?.id;
    if (!artifactId) return;
    scheduleDetailWaveform();
    try {
      for (let attempt = 0; attempt < 40; attempt += 1) {
        const result = await artifactApi.waveform(artifactId, 5000);
        if (Array.isArray(result.points)) {
          peaks = result.points;
          scheduleDetailWaveform();
          return;
        }
        await new Promise((resolve) => window.setTimeout(resolve, 750));
      }
    } catch {
      peaks = [];
    }
  }

  function scheduleDetailWaveform() {
    if (!plan) return;
    const bucketMs = 30_000;
    const spanMs = Math.min(60_000, plan.duration_ms);
    const startMs = Math.max(
      0,
      Math.min(
        plan.duration_ms - spanMs,
        Math.floor(currentMs / bucketMs) * bucketMs - bucketMs / 2
      )
    );
    const endMs = Math.min(plan.duration_ms, startMs + spanMs);
    const key = `${plan.source_media_artifact.id}:${startMs}:${endMs}`;
    if (key === detailKey || key === pendingDetailKey) return;
    if (detailTimer !== undefined) window.clearTimeout(detailTimer);
    pendingDetailKey = key;
    detailTimer = window.setTimeout(
      () => void loadDetailWaveform(key, startMs, endMs),
      220
    );
  }

  async function loadDetailWaveform(
    key: string,
    startMs: number,
    endMs: number
  ) {
    if (!plan || key === detailKey) return;
    detailRequest?.abort();
    const controller = new AbortController();
    detailRequest = controller;
    try {
      for (let attempt = 0; attempt < 40; attempt += 1) {
        const result = await artifactApi.waveformWindow(
          plan.source_media_artifact.id,
          startMs,
          endMs,
          3000,
          controller.signal
        );
        if (Array.isArray(result.points)) {
          detailPeaks = result.points;
          detailPeaksStartMs = Number(result.start_ms ?? startMs);
          detailPeaksEndMs = Number(result.end_ms ?? endMs);
          detailKey = key;
          return;
        }
        await new Promise((resolve) => window.setTimeout(resolve, 500));
      }
    } catch (caught) {
      if (!(caught instanceof DOMException && caught.name === 'AbortError')) {
        detailPeaks = [];
      }
    } finally {
      if (pendingDetailKey === key) pendingDetailKey = '';
    }
  }

  async function openProposal() {
    proposalOpen = true;
    if (proposalModels.length || proposalModelsLoading) return;
    proposalModelsLoading = true;
    proposalModelsError = '';
    try {
      const providerPayload = await sessionApi.providers();
      const enabled = providerPayload.items.filter(
        (provider) => provider.enabled
      );
      const groups = await Promise.all(
        enabled.map(async (provider) => ({
          provider,
          models: (await sessionApi.providerModels(provider.id)).items
        }))
      );
      proposalModels = groups.flatMap(({ provider, models }) =>
        models
          .filter((item) => item.is_active)
          .map((item) => {
            const custom =
              Boolean(provider.options_json?.is_custom) ||
              !['openai', 'gemini', 'anthropic'].includes(
                provider.provider_key
              );
            const providerId = custom
              ? provider.options_json?.provider_id || provider.id
              : provider.options_json?.provider_id || provider.provider_key;
            return {
              value: custom
                ? `custom:${providerId}/${item.model_id}`
                : `${provider.provider_key}/${item.model_id}`,
              label: `${provider.label} · ${item.model_id}`,
              isDefault: Boolean(item.is_default)
            };
          })
      );
    } catch (caught) {
      proposalModelsError = errorMessage(caught);
    } finally {
      proposalModelsLoading = false;
    }
  }

  async function prepare(force = false) {
    busy = 'prepare';
    error = '';
    message = '';
    try {
      adopt(await mediaEditApi.prepare(sessionId, force));
      message = force
        ? 'The timeline was rebuilt from the current source inputs.'
        : 'The editable timeline is ready.';
      void loadWaveform();
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      busy = '';
    }
  }

  async function save(reviewed = plan?.reviewed ?? false) {
    if (!plan) return null;
    const keep = keepFromCuts(cuts, plan.duration_ms);
    if (!keep.length) {
      error = 'At least one part of the recording must remain.';
      return null;
    }
    busy = 'save';
    error = '';
    message = '';
    try {
      const next = await mediaEditApi.update(sessionId, plan.revision, {
        keep_ranges: keep,
        instructions,
        reviewed
      });
      adopt(next);
      message = `Saved edit plan revision ${next.plan?.revision}.`;
      return next.plan ?? null;
    } catch (caught) {
      error = errorMessage(caught);
      return null;
    } finally {
      busy = '';
    }
  }

  async function watchJob(job: JobRecord, successMessage: string) {
    activeJob = job;
    const poll = async () => {
      try {
        const current = await jobApi.get(job.id);
        activeJob = current;
        if (
          ['queued', 'running', 'cancel_requested'].includes(current.status)
        ) {
          pollTimer = window.setTimeout(poll, 900);
          return;
        }
        if (current.status === 'completed') {
          await load();
          message = successMessage;
        } else {
          error = current.error_message || `The ${current.kind} job failed.`;
        }
      } catch (caught) {
        error = errorMessage(caught);
      } finally {
        if (activeJob && !['queued', 'running'].includes(activeJob.status))
          busy = '';
      }
    };
    await poll();
  }

  async function propose() {
    if (!plan || !instructions.trim()) return;
    busy = 'propose';
    error = '';
    message = '';
    try {
      const saved = await save(false);
      if (!saved) return;
      busy = 'propose';
      proposalOpen = false;
      await watchJob(
        await mediaEditApi.propose(
          sessionId,
          saved.revision,
          instructions,
          proposalModel === 'default' ? undefined : proposalModel
        ),
        'The agent proposal is ready. Review every red range before rendering.'
      );
    } catch (caught) {
      error = errorMessage(caught);
      busy = '';
    }
  }

  async function render() {
    if (!plan) return;
    busy = 'render';
    error = '';
    message = '';
    try {
      const saved = await save(true);
      if (!saved) return;
      busy = 'render';
      await watchJob(
        await mediaEditApi.render(sessionId, saved.revision),
        'Rendered media and retimed subtitles are ready. The edited media is now the session output source.'
      );
    } catch (caught) {
      error = errorMessage(caught);
      busy = '';
    }
  }

  function seek(timeMs: number) {
    currentMs = Math.max(0, Math.min(plan?.duration_ms ?? 0, timeMs));
    if (video) video.currentTime = currentMs / 1000;
    scheduleDetailWaveform();
  }

  function updatePlayback() {
    if (!video) return;
    currentMs = Math.round(video.currentTime * 1000);
    scheduleDetailWaveform();
    if (loopEndMs != null && currentMs >= loopEndMs) {
      video.pause();
      loopEndMs = null;
    }
  }

  function stopPlaybackTracking() {
    if (playbackFrame !== undefined) {
      window.cancelAnimationFrame(playbackFrame);
      playbackFrame = undefined;
    }
  }

  function trackPlayback() {
    stopPlaybackTracking();
    const tick = () => {
      updatePlayback();
      if (video && !video.paused && !video.ended)
        playbackFrame = window.requestAnimationFrame(tick);
      else playbackFrame = undefined;
    };
    tick();
  }

  function audition(timeMs: number) {
    if (!video) return;
    seek(Math.max(0, timeMs - 1300));
    loopEndMs = Math.min(plan?.duration_ms ?? timeMs + 1300, timeMs + 1300);
    void video.play();
  }

  function addCut() {
    if (!plan || cutStartMs == null || cutEndMs == null) return;
    const start = Math.max(0, Math.min(cutStartMs, cutEndMs));
    const end = Math.min(plan.duration_ms, Math.max(cutStartMs, cutEndMs));
    if (end - start < 20) {
      error = 'A cut must be at least 20 ms long.';
      return;
    }
    cuts = [
      ...cuts,
      {
        id: `cut-manual-${(cutSequence += 1)}`,
        start_ms: start,
        end_ms: end,
        label: 'Manual cut'
      }
    ].sort((a, b) => a.start_ms - b.start_ms);
    cutStartMs = null;
    cutEndMs = null;
  }

  function nudge(index: number, edge: 'start_ms' | 'end_ms', amount: number) {
    cuts = cuts.map((range, rangeIndex) =>
      rangeIndex === index
        ? {
            ...range,
            [edge]: Math.max(
              0,
              Math.min(plan?.duration_ms ?? 0, range[edge] + amount)
            )
          }
        : range
    );
  }

  onMount(load);
  onDestroy(() => {
    if (pollTimer !== undefined) window.clearTimeout(pollTimer);
    if (detailTimer !== undefined) window.clearTimeout(detailTimer);
    stopPlaybackTracking();
    detailRequest?.abort();
    if (captionTrackUrl) URL.revokeObjectURL(captionTrackUrl);
  });
</script>

<div class="min-w-0 max-w-full space-y-5 overflow-x-hidden">
  <header class="flex flex-wrap items-end justify-between gap-4">
    <div class="min-w-0">
      <div class="eyebrow">Transcript-guided edit</div>
      <h2 class="mt-1 text-2xl font-semibold">
        Cut the recording, not your patience
      </h2>
      <p class="muted mt-2 max-w-3xl text-sm">
        Red ranges are removed. Pandrator stores every plan revision and only
        creates new media when you explicitly render the reviewed timeline.
      </p>
    </div>
    {#if plan}<div class="flex flex-wrap gap-2">
        <button
          onclick={() => prepare(true)}
          disabled={Boolean(busy)}
          title="Re-read the current recording, captions, and optional ASR timing into a new editable plan."
          class="secondary"><RefreshCw size={15} /> Rebuild</button
        ><button
          onclick={() => save()}
          disabled={Boolean(busy)}
          title="Save the current cut ranges as a new reversible plan revision without rendering media."
          class="secondary"><Save size={15} /> Save</button
        ><a
          href={`/sessions/${sessionId}?settings=transcribe`}
          title="Choose the recognition model used to align attached captions while preserving their wording and speakers."
          class="secondary"><Settings2 size={15} /> Alignment</a
        ><button
          onclick={openProposal}
          disabled={Boolean(busy)}
          title="Choose an LLM and ask it to propose removable transcript spans. Nothing is rendered automatically."
          class="secondary"><Sparkles size={15} /> Process with LLM</button
        ><button
          onclick={render}
          disabled={Boolean(busy)}
          title="Mark this exact plan reviewed and create edited media plus retimed subtitles."
          class="primary"
          >{#if busy === 'render'}<LoaderCircle
              class="animate-spin"
              size={16}
            />{:else}<Scissors size={16} />{/if} Render</button
        >
      </div>{/if}
  </header>

  {#if error}<div role="alert" class="alert error">
      <CircleAlert size={18} /> <span>{error}</span>
    </div>{/if}
  {#if message}<div class="alert success">
      <Check size={18} /> <span>{message}</span>
    </div>{/if}
  {#if activeJob && ['queued', 'running', 'cancel_requested'].includes(activeJob.status)}<div
      class="surface rounded-2xl p-4"
    >
      <div class="flex items-center gap-3">
        <LoaderCircle class="animate-spin text-[var(--accent)]" size={18} />
        <div class="min-w-0 flex-1">
          <div class="text-sm font-semibold capitalize">
            {activeJob.progress_detail || activeJob.kind.replaceAll('.', ' ')}
          </div>
          <div class="mt-2 h-1.5 overflow-hidden rounded-full bg-[var(--line)]">
            <div
              class="h-full bg-[var(--accent)]"
              style={`width:${Math.max(2, activeJob.progress * 100)}%`}
            ></div>
          </div>
        </div>
        <span class="muted text-xs tabular-nums"
          >{Math.round(activeJob.progress * 100)}%</span
        >
      </div>
    </div>{/if}

  {#if !workspaceState}<section
      class="surface grid min-h-56 place-items-center rounded-2xl"
    >
      <LoaderCircle class="animate-spin text-[var(--accent)]" />
    </section>
  {:else if !plan}<section class="surface rounded-2xl p-6 sm:p-8">
      <div
        class="grid min-w-0 gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,.85fr)]"
      >
        <div class="min-w-0">
          <div class="eyebrow">Inputs</div>
          <h3 class="mt-1 text-xl font-semibold">
            Prepare the editable timeline
          </h3>
          <p class="muted mt-3 text-sm leading-relaxed">
            Pandrator needs a recording plus either attached captions or a
            completed ASR transcription. When both exist, the caption wording
            and speakers stay authoritative while ASR supplies finer timing.
            Aligned words remain available in the edit plan and the rendered
            native subtitle document.
          </p>
          <button
            onclick={() => prepare(false)}
            disabled={Boolean(busy) || !workspaceState.readiness.ready}
            class="primary mt-5"
            >{#if busy === 'prepare'}<LoaderCircle
                class="animate-spin"
                size={16}
              />{:else}<ChevronRight size={16} />{/if} Prepare timeline</button
          >
          {#if workspaceState.readiness.source_media_artifact && !workspaceState.readiness.external_transcript_artifact && !workspaceState.readiness.transcription_artifact}<a
              href={`/sessions/${sessionId}`}
              class="secondary mt-3 inline-flex"
              >Open Overview to run ASR first</a
            >{/if}
        </div>
        <div class="min-w-0 space-y-2">
          {@render InputStatus(
            'Recording',
            Boolean(workspaceState.readiness.source_media_artifact),
            workspaceState.readiness.source_media_artifact?.filename ??
              'Attach video or audio in Sources'
          )}
          {@render InputStatus(
            'Editorial transcript',
            Boolean(
              workspaceState.readiness.external_transcript_artifact ||
              workspaceState.readiness.transcription_artifact
            ),
            workspaceState.readiness.external_transcript_artifact?.filename ??
              workspaceState.readiness.transcription_artifact?.filename ??
              'Attach Zoom captions or run Transcribe'
          )}
          {@render InputStatus(
            'Caption word alignment',
            Boolean(workspaceState.readiness.timing_artifact),
            workspaceState.readiness.timing_artifact?.filename ??
              'Optional: configure Alignment to project ASR word timing onto captions'
          )}
        </div>
      </div>
    </section>
  {:else}<div
      class="grid min-w-0 max-w-full gap-5 2xl:grid-cols-[minmax(0,1.55fr)_minmax(23rem,.65fr)]"
    >
      <div class="min-w-0 space-y-5">
        <section class="surface overflow-hidden rounded-2xl">
          <video
            bind:this={video}
            src={plan.source_media_artifact.content_url}
            controls
            preload="metadata"
            class="aspect-video max-h-[68vh] w-full bg-black object-contain"
            ontimeupdate={updatePlayback}
            onseeked={updatePlayback}
            onplay={trackPlayback}
            onpause={stopPlaybackTracking}
            onended={stopPlaybackTracking}
          >
            <track
              kind="captions"
              src={captionTrackUrl}
              srclang="und"
              label="Session transcript"
              default
            />
          </video>
          <div class="space-y-4 p-4 sm:p-5">
            <div
              class="flex flex-wrap items-center justify-between gap-3 text-xs"
            >
              <span class="font-semibold tabular-nums"
                >{formatTime(currentMs)}</span
              >
              <span class="muted"
                >Keep {formatTime(keptDurationMs)} of {formatTime(
                  plan.duration_ms
                )} · remove {cuts.length} range{cuts.length === 1
                  ? ''
                  : 's'}</span
              >
            </div>
            <MediaTimeline
              durationMs={plan.duration_ms}
              {currentMs}
              keepRanges={keepFromCuts(cuts, plan.duration_ms)}
              {peaks}
              {detailPeaks}
              {detailPeaksStartMs}
              {detailPeaksEndMs}
              detailLoading={Boolean(pendingDetailKey)}
              onseek={seek}
            />
            <div
              class="flex flex-wrap items-center gap-2 rounded-xl bg-[var(--paper)] p-3"
            >
              <button onclick={() => (cutStartMs = currentMs)} class="marker">
                Set cut start<br /><small
                  >{cutStartMs == null
                    ? 'at playhead'
                    : formatTime(cutStartMs)}</small
                >
              </button>
              <button onclick={() => (cutEndMs = currentMs)} class="marker">
                Set cut end<br /><small
                  >{cutEndMs == null
                    ? 'at playhead'
                    : formatTime(cutEndMs)}</small
                >
              </button>
              <button
                onclick={addCut}
                disabled={cutStartMs == null || cutEndMs == null}
                class="primary disabled:opacity-40"
                ><Plus size={15} /> Add cut</button
              >
            </div>
          </div>
        </section>

        <section class="surface rounded-2xl p-5">
          <div class="flex flex-wrap items-start justify-between gap-3">
            <div>
              <div class="eyebrow">Removal list</div>
              <h3 class="mt-1 text-lg font-semibold">Exact cut boundaries</h3>
            </div>
            <span class="badge">revision {plan.revision}</span>
          </div>
          <div class="mt-4 space-y-3">
            {#each cuts as cut, index (cut.id)}<article class="cut-card">
                <div class="grid min-w-0 flex-1 gap-3 sm:grid-cols-2">
                  <label
                    >Start
                    <input
                      type="number"
                      min="0"
                      max={plan.duration_ms}
                      step="20"
                      bind:value={cut.start_ms}
                    />
                    <small>{formatTime(cut.start_ms)}</small></label
                  ><label
                    >End
                    <input
                      type="number"
                      min="0"
                      max={plan.duration_ms}
                      step="20"
                      bind:value={cut.end_ms}
                    />
                    <small>{formatTime(cut.end_ms)}</small></label
                  >
                  <input
                    aria-label={`Label for cut ${index + 1}`}
                    bind:value={cut.label}
                    class="sm:col-span-2"
                  />
                  <div class="sm:col-span-2 flex flex-wrap gap-1">
                    <button onclick={() => nudge(index, 'start_ms', -100)}
                      >-100 start</button
                    >
                    <button onclick={() => nudge(index, 'start_ms', 100)}
                      >+100 start</button
                    >
                    <button onclick={() => audition(cut.start_ms)}
                      ><Play size={12} /> start</button
                    >
                    <button onclick={() => nudge(index, 'end_ms', -100)}
                      >-100 end</button
                    >
                    <button onclick={() => nudge(index, 'end_ms', 100)}
                      >+100 end</button
                    >
                    <button onclick={() => audition(cut.end_ms)}
                      ><Play size={12} /> end</button
                    >
                  </div>
                </div>
                <button
                  onclick={() =>
                    (cuts = cuts.filter((_, value) => value !== index))}
                  class="delete"
                  aria-label={`Delete cut ${index + 1}`}
                  ><Trash2 size={15} /></button
                >
              </article>{:else}<div
                class="muted rounded-xl border border-dashed border-[var(--line)] p-6 text-center text-sm"
              >
                Nothing is removed. Set two playhead markers or ask the agent
                for a proposal.
              </div>{/each}
          </div>
        </section>
      </div>

      <aside class="min-w-0 space-y-5">
        {#if plan.evidence.warnings?.length}<section
            class="rounded-2xl border border-amber-400/40 bg-amber-500/10 p-4"
          >
            <div class="flex gap-2 text-sm font-semibold">
              <CircleAlert size={17} /> Timing review needed
            </div>
            <ul class="muted mt-2 list-disc space-y-1 pl-5 text-xs">
              {#each plan.evidence.warnings as warning}<li>{warning}</li>{/each}
            </ul>
          </section>{/if}

        <section class="surface rounded-2xl p-5">
          <div class="flex items-center justify-between gap-3">
            <div>
              <div class="eyebrow">Transcript</div>
              <h3 class="mt-1 font-semibold">
                {plan.cues.length} timed cues
              </h3>
            </div>
            <Clock3 class="muted" size={17} />
          </div>
          <label
            class="mt-4 flex items-center gap-2 rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3"
            ><Search class="muted" size={15} /><span class="sr-only"
              >Search transcript</span
            ><input
              bind:value={search}
              placeholder="Search words or speakers"
              class="w-full bg-transparent py-2.5 text-sm outline-none"
            /></label
          >
          <div class="transcript mt-3 space-y-1">
            {#each displayedCues as cue (cue.id)}<button
                onclick={() => seek(cue.start_ms)}
                class:active={cue.id === activeCue?.id}
                class="cue"
              >
                <span class="time">{formatTime(cue.start_ms)}</span>
                <span class="min-w-0">
                  {#if cue.speaker}<strong>{cue.speaker}</strong>{/if}
                  <span>{cue.text}</span>
                  <small
                    >{cue.timing_source.replaceAll('_', ' ')} · {Math.round(
                      (cue.timing_confidence ?? 0) * 100
                    )}%</small
                  >
                </span>
              </button>{/each}
          </div>
        </section>

        {#if activeJob?.status === 'completed' && activeJob.result_json?.media_artifact_id}<section
            class="surface rounded-2xl p-5"
          >
            <h3 class="font-semibold">Latest rendered edit</h3>
            <div class="mt-3 flex flex-wrap gap-2">
              <a
                class="secondary"
                href={`/api/v1/artifacts/${activeJob.result_json.media_artifact_id}/content`}
                download><Download size={15} /> Media</a
              ><a
                class="secondary"
                href={`/api/v1/artifacts/${activeJob.result_json.subtitle_artifact_id}/content`}
                download><Download size={15} /> Subtitles</a
              >
            </div>
          </section>{/if}
      </aside>
    </div>{/if}
</div>

{#if proposalOpen}
  <div
    class="fixed inset-0 z-50 grid place-items-center bg-black/40 p-4 backdrop-blur-sm sm:p-6"
    role="presentation"
    onclick={(event) =>
      event.target === event.currentTarget && (proposalOpen = false)}
  >
    <div
      use:modalFocus={{
        onclose: () => (proposalOpen = false),
        initialFocus: '#media-edit-instructions'
      }}
      class="surface max-h-[92vh] w-full max-w-2xl overflow-y-auto rounded-[1.7rem] p-5 sm:p-7"
      role="dialog"
      aria-modal="true"
      aria-labelledby="proposal-title"
    >
      <div class="flex items-start justify-between gap-5">
        <div class="min-w-0">
          <div class="eyebrow">Transcript-guided edit</div>
          <h2 id="proposal-title" class="mt-1 text-2xl font-semibold">
            Process with LLM
          </h2>
          <p class="muted mt-2 text-sm leading-relaxed">
            The model proposes removable transcript spans. Pandrator refines
            their edges against word timing and speech gaps; you still review
            every red range before rendering.
          </p>
        </div>
        <button
          onclick={() => (proposalOpen = false)}
          aria-label="Close LLM proposal settings"
          class="rounded-lg p-2"><X size={19} /></button
        >
      </div>
      <div class="mt-6 grid gap-5">
        <label class="text-sm font-semibold" for="media-edit-instructions"
          >Editing instructions<textarea
            id="media-edit-instructions"
            bind:value={instructions}
            rows="8"
            class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] p-3 font-normal leading-relaxed text-[var(--ink)]"
          ></textarea></label
        >
        <label class="text-sm font-semibold"
          >LLM model<select
            bind:value={proposalModel}
            disabled={proposalModelsLoading}
            class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal disabled:opacity-50"
            ><option value="default"
              >{proposalModelsLoading
                ? 'Loading configured models…'
                : 'Application default'}</option
            >{#each proposalModels as item}<option value={item.value}
                >{item.label}{item.isDefault ? ' · default' : ''}</option
              >{/each}</select
          ></label
        >
        {#if proposalModelsError}<p role="alert" class="text-xs text-red-500">
            {proposalModelsError}
          </p>{/if}
        <a
          href="/providers"
          class="w-fit text-xs font-semibold text-[var(--accent)]"
          >Manage LLM models and connections</a
        >
      </div>
      <div class="mt-7 flex flex-wrap justify-end gap-3">
        <button onclick={() => (proposalOpen = false)} class="secondary"
          >Cancel</button
        ><button
          onclick={propose}
          disabled={Boolean(busy) || !instructions.trim()}
          class="primary disabled:opacity-40"
          >{#if busy === 'propose'}<LoaderCircle
              class="animate-spin"
              size={16}
            />{:else}<Bot size={16} />{/if} Generate proposal</button
        >
      </div>
    </div>
  </div>
{/if}

{#snippet InputStatus(label: string, ready: boolean, detail: string)}
  <div
    class="flex min-w-0 items-start gap-3 overflow-hidden rounded-xl border border-[var(--line)] p-3"
  >
    <span class:ready class="status-dot"></span>
    <div class="min-w-0">
      <strong class="text-sm">{label}</strong>
      <p class="muted mt-0.5 break-all text-xs sm:truncate">{detail}</p>
    </div>
  </div>
{/snippet}

<style>
  .primary,
  .secondary {
    display: flex;
    align-items: center;
    gap: 0.45rem;
    border-radius: 0.75rem;
    padding: 0.65rem 0.85rem;
    font-size: 0.78rem;
    font-weight: 700;
  }
  .primary {
    background: var(--action-bg);
    color: white;
  }
  .primary:hover {
    background: var(--action-hover);
  }
  .secondary {
    border: 1px solid var(--line);
    background: var(--paper-strong);
  }
  .alert {
    display: flex;
    align-items: flex-start;
    gap: 0.6rem;
    border-radius: 0.9rem;
    padding: 0.8rem 1rem;
    font-size: 0.8rem;
  }
  .alert.error {
    background: rgb(220 75 75 / 0.1);
    color: #dc4b4b;
  }
  .alert.success {
    background: var(--accent-soft);
  }
  .marker {
    min-width: 8.5rem;
    border: 1px solid var(--line);
    border-radius: 0.7rem;
    padding: 0.55rem 0.7rem;
    text-align: left;
    font-size: 0.72rem;
    font-weight: 700;
  }
  .marker small {
    color: var(--muted);
    font-weight: 500;
  }
  .badge {
    border-radius: 999px;
    background: var(--accent-soft);
    padding: 0.3rem 0.6rem;
    color: var(--accent);
    font-size: 0.65rem;
    font-weight: 750;
    text-transform: uppercase;
  }
  .cut-card {
    display: flex;
    align-items: flex-start;
    gap: 0.75rem;
    border: 1px solid rgb(220 75 75 / 0.25);
    border-radius: 0.9rem;
    background: rgb(220 75 75 / 0.04);
    padding: 0.8rem;
  }
  .cut-card label {
    color: var(--muted);
    font-size: 0.65rem;
    font-weight: 700;
    text-transform: uppercase;
  }
  .cut-card input {
    width: 100%;
    border: 1px solid var(--line);
    border-radius: 0.55rem;
    background: var(--paper-strong);
    padding: 0.45rem 0.55rem;
    color: var(--ink);
    font-size: 0.75rem;
    text-transform: none;
  }
  .cut-card label small {
    display: block;
    margin-top: 0.25rem;
    font-variant-numeric: tabular-nums;
    text-transform: none;
  }
  .cut-card div button,
  .delete {
    display: inline-flex;
    align-items: center;
    gap: 0.25rem;
    border: 1px solid var(--line);
    border-radius: 0.5rem;
    padding: 0.35rem 0.45rem;
    font-size: 0.65rem;
    font-weight: 650;
  }
  .delete {
    color: #dc4b4b;
  }
  .status-dot {
    margin-top: 0.3rem;
    width: 0.55rem;
    height: 0.55rem;
    flex: none;
    border-radius: 999px;
    background: #d29a37;
  }
  .status-dot.ready {
    background: #45a66c;
  }
  .transcript {
    max-height: 42rem;
    overflow-y: auto;
    overscroll-behavior: contain;
  }
  .cue {
    display: grid;
    width: 100%;
    grid-template-columns: 4.8rem 1fr;
    gap: 0.6rem;
    border-radius: 0.65rem;
    padding: 0.55rem;
    text-align: left;
    content-visibility: auto;
    contain-intrinsic-size: 4rem;
  }
  .cue:hover,
  .cue.active {
    background: var(--accent-soft);
  }
  .cue .time {
    color: var(--muted);
    font-size: 0.65rem;
    font-variant-numeric: tabular-nums;
  }
  .cue strong,
  .cue span,
  .cue small {
    display: block;
  }
  .cue strong {
    margin-bottom: 0.12rem;
    color: var(--accent);
    font-size: 0.65rem;
  }
  .cue span {
    font-size: 0.78rem;
    line-height: 1.45;
  }
  .cue small {
    margin-top: 0.25rem;
    color: var(--muted);
    font-size: 0.58rem;
  }
</style>
