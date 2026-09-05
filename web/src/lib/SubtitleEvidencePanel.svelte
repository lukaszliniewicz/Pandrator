<script lang="ts">
  import {
    AlertTriangle,
    AudioLines,
    Check,
    CircleHelp,
    Cloud,
    LoaderCircle,
    RotateCcw,
    ShieldQuestion,
    Trash2
  } from '@lucide/svelte';
  import { onDestroy, onMount } from 'svelte';
  import AudioPlayer from './AudioPlayer.svelte';
  import { sessionApi } from './domain-api';
  import { errorMessage } from './errors';
  import type {
    SubtitleEvidenceCandidate,
    SubtitleEvidenceRecord,
    SubtitleEvidenceRoute,
    ProviderModelRecord,
    ProviderRecord,
    SubtitleSegment
  } from './api-models';

  let {
    sessionId,
    sourceArtifactId,
    segment,
    records,
    onupdated,
    onaccept,
    ondelete,
    onuncertain,
    onclear
  }: {
    sessionId: string;
    sourceArtifactId: string;
    segment: SubtitleSegment;
    records: SubtitleEvidenceRecord[];
    onupdated: (record: SubtitleEvidenceRecord) => void;
    onaccept: (
      candidate: SubtitleEvidenceCandidate,
      evidenceId: string
    ) => void;
    ondelete: (evidenceId?: string) => void;
    onuncertain: (reason: string, evidenceId?: string) => void;
    onclear: () => void;
  } = $props();

  const routeOptions: Array<{
    id: SubtitleEvidenceRoute;
    label: string;
    description: string;
    commercial?: boolean;
  }> = [
    {
      id: 'whisper',
      label: 'Whisper',
      description: 'Local independent ASR with word timing.'
    },
    {
      id: 'moss',
      label: 'MOSS',
      description: 'Local transcription plus alignment.'
    },
    {
      id: 'azure_mai_transcribe_2',
      label: 'MAI-Transcribe-2',
      description: 'Azure preview model with native word timing.',
      commercial: true
    },
    {
      id: 'audio_llm',
      label: 'Audio-native LLM',
      description:
        'A configured multimodal model returns an untimed transcript for the bounded clip.'
    }
  ];

  type AudioModelOption = {
    id: string;
    modelId: string;
    providerLabel: string;
    custom: boolean;
  };

  let selectedRoutes = $state<SubtitleEvidenceRoute[]>(['whisper', 'moss']);
  let audioModels = $state<AudioModelOption[]>([]);
  let selectedAudioModelIds = $state<string[]>([]);
  let loadingAudioModels = $state(false);
  let audioModelsError = $state('');
  let reason = $state('');
  let reasonSegmentId = '';
  let requesting = $state(false);
  let resolving = $state(false);
  let error = $state('');
  let polledRecord = $state<SubtitleEvidenceRecord | null>(null);
  let pollingTimer: ReturnType<typeof setTimeout> | null = null;
  let pollCount = 0;

  $effect(() => {
    const nextSegmentId = segment.id ?? '';
    if (reasonSegmentId !== nextSegmentId) {
      reason =
        segment.review_note ||
        'The cue is semantically inconsistent with the surrounding utterance.';
      reasonSegmentId = nextSegmentId;
    }
  });

  const latestRecord = $derived(
    polledRecord ??
      records
        .slice()
        .sort((left, right) =>
          right.created_at.localeCompare(left.created_at)
        )[0] ??
      null
  );
  const candidates = $derived(
    latestRecord?.candidates ?? latestRecord?.candidates_json ?? []
  );
  const active = $derived(
    latestRecord?.status === 'queued' || latestRecord?.status === 'running'
  );

  function evidenceId(record: SubtitleEvidenceRecord) {
    return record.evidence_id || record.id;
  }

  function toggleRoute(route: SubtitleEvidenceRoute) {
    selectedRoutes = selectedRoutes.includes(route)
      ? selectedRoutes.filter((item) => item !== route)
      : [...selectedRoutes, route];
    if (route === 'audio_llm' && !selectedRoutes.includes('audio_llm'))
      selectedAudioModelIds = [];
  }

  function toggleAudioModel(modelId: string) {
    selectedAudioModelIds = selectedAudioModelIds.includes(modelId)
      ? selectedAudioModelIds.filter((item) => item !== modelId)
      : [...selectedAudioModelIds, modelId].slice(0, 3);
  }

  async function loadAudioModels() {
    loadingAudioModels = true;
    audioModelsError = '';
    try {
      const providerPage = await sessionApi.providers();
      const providers = (providerPage.items as ProviderRecord[]).filter(
        (provider) => provider.enabled && (provider.kind ?? 'llm') === 'llm'
      );
      const modelPages = await Promise.all(
        providers.map(async (provider) => ({
          provider,
          models: (await sessionApi.providerModels(provider.id))
            .items as ProviderModelRecord[]
        }))
      );
      audioModels = modelPages.flatMap(({ provider, models }) =>
        models
          .filter(
            (model) =>
              (model.is_active || model.is_default) &&
              (model.supports_audio_input ||
                model.input_modalities?.includes('audio'))
          )
          .map((model) => ({
            id: model.id,
            modelId: model.model_id,
            providerLabel: provider.label,
            custom: Boolean(provider.options_json?.is_custom)
          }))
      );
    } catch (caught) {
      audioModelsError = errorMessage(caught);
    } finally {
      loadingAudioModels = false;
    }
  }

  function stopPolling() {
    if (pollingTimer) clearTimeout(pollingTimer);
    pollingTimer = null;
  }

  function schedulePoll(record: SubtitleEvidenceRecord) {
    stopPolling();
    if (!['queued', 'running'].includes(record.status) || pollCount >= 400)
      return;
    pollingTimer = setTimeout(async () => {
      try {
        const next = await sessionApi.subtitleEvidenceRecord(
          evidenceId(record)
        );
        polledRecord = next;
        onupdated(next);
        pollCount += 1;
        schedulePoll(next);
      } catch (caught) {
        error = errorMessage(caught);
      }
    }, 1500);
  }

  onDestroy(stopPolling);
  onMount(() => {
    void loadAudioModels();
  });

  async function requestEvidence() {
    if (!segment.id || !selectedRoutes.length || !reason.trim()) return;
    requesting = true;
    error = '';
    stopPolling();
    pollCount = 0;
    try {
      const response = await sessionApi.requestSubtitleEvidence(sessionId, {
        source_artifact_id: sourceArtifactId,
        cue_id: segment.ordinal + 1,
        reason: reason.trim(),
        routes: selectedRoutes,
        audio_model_ids: selectedAudioModelIds,
        padding_before_ms: 2000,
        padding_after_ms: 2000
      });
      const record = response.evidence;
      polledRecord = record;
      onupdated(record);
      schedulePoll(record);
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      requesting = false;
    }
  }

  async function resolve(
    action: 'accepted' | 'deleted' | 'uncertain' | 'dismissed',
    candidate?: SubtitleEvidenceCandidate
  ) {
    const record = latestRecord;
    if (!record) {
      if (action === 'deleted') ondelete();
      if (action === 'uncertain') onuncertain(reason.trim());
      if (action === 'dismissed') onclear();
      return;
    }
    resolving = true;
    error = '';
    try {
      const updated = await sessionApi.resolveSubtitleEvidence(
        sessionId,
        evidenceId(record),
        {
          action,
          candidate_id: candidate?.id,
          note: reason.trim()
        }
      );
      polledRecord = updated;
      onupdated(updated);
      if (action === 'accepted' && candidate)
        onaccept(candidate, evidenceId(record));
      if (action === 'deleted') ondelete(evidenceId(record));
      if (action === 'uncertain')
        onuncertain(reason.trim(), evidenceId(record));
      if (action === 'dismissed') onclear();
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      resolving = false;
    }
  }

  function routeLabel(route: SubtitleEvidenceRoute) {
    return routeOptions.find((option) => option.id === route)?.label ?? route;
  }

  function audioReady() {
    return (
      !selectedRoutes.includes('audio_llm') || selectedAudioModelIds.length > 0
    );
  }

  function transportLabel(candidate: SubtitleEvidenceCandidate) {
    const transport = candidate.transport;
    if (!transport) return '';
    const wire = transport.provider_wire_mapping?.replaceAll('_', ' ') ?? '';
    const consumption =
      transport.audio_consumption === 'confirmed'
        ? 'audio use confirmed by tokens'
        : 'provider did not report audio tokens';
    return [wire, consumption].filter(Boolean).join(' · ');
  }

  function clock(milliseconds: number) {
    return `${(milliseconds / 1000).toFixed(2)} s`;
  }

  function confidence(candidate: SubtitleEvidenceCandidate) {
    const scores = (
      candidate.words ??
      candidate.segments?.flatMap((item) => item.words ?? []) ??
      []
    )
      .map((word) => word.confidence)
      .filter((value): value is number => typeof value === 'number');
    if (!scores.length) return '';
    return `${Math.round((scores.reduce((sum, value) => sum + value, 0) / scores.length) * 100)}% mean word confidence`;
  }

  function costLabel(candidate: SubtitleEvidenceCandidate) {
    const cost = candidate.cost;
    if (cost?.kind === 'not_applicable') return 'Local · no provider charge';
    if (!cost || !['actual', 'estimate'].includes(cost.kind ?? ''))
      return 'Provider cost unavailable';
    const amount = Number(cost.amount ?? cost.usd);
    if (!Number.isFinite(amount)) return 'Estimated cost unavailable';
    const prefix = cost.kind === 'actual' ? 'Cost' : 'Estimated';
    return `${prefix} ${cost.currency || 'USD'} ${amount.toFixed(6)}`;
  }
</script>

<section
  class="evidence-panel mt-3 rounded-xl border border-[var(--line)] bg-[var(--paper-strong)] p-3"
>
  <div class="flex flex-wrap items-start justify-between gap-2">
    <div>
      <div class="flex items-center gap-2 text-xs font-semibold">
        <ShieldQuestion size={15} class="text-[var(--accent)]" /> Audio evidence
        {#if active}<span class="status running"
            ><LoaderCircle size={11} class="animate-spin" /> Running</span
          >
        {:else if latestRecord}<span
            class:warning={latestRecord.status === 'failed' ||
              latestRecord.status === 'uncertain'}
            class="status">{latestRecord.status}</span
          >{/if}
      </div>
      <p class="muted mt-1 text-[.68rem]">
        Recheck only this cue plus two seconds of context. The witnesses advise;
        you still make the editorial decision.
      </p>
    </div>
    {#if segment.review_state === 'uncertain'}
      <button
        class="quiet-action"
        disabled={resolving || active}
        onclick={() => resolve('dismissed')}
        ><Check size={13} /> Clear uncertainty</button
      >
    {/if}
  </div>

  <label class="mt-3 block text-[.68rem] font-semibold">
    Why this needs another listen
    <textarea
      bind:value={reason}
      rows="2"
      maxlength="4000"
      class="mt-1 w-full resize-y rounded-lg border border-[var(--line)] bg-[var(--paper)] p-2 text-xs font-normal"
    ></textarea>
  </label>

  <div class="mt-3 grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
    {#each routeOptions as option}
      <label
        class:selected={selectedRoutes.includes(option.id)}
        class="route-option"
      >
        <input
          type="checkbox"
          checked={selectedRoutes.includes(option.id)}
          onchange={() => toggleRoute(option.id)}
        />
        <span class="min-w-0">
          <strong>{option.label}</strong>
          {#if option.commercial}<span class="commercial"
              ><Cloud size={10} /> commercial</span
            >{/if}
          <small>{option.description}</small>
        </span>
      </label>
    {/each}
  </div>

  {#if selectedRoutes.includes('audio_llm')}
    <div
      class="mt-3 rounded-xl border border-[var(--line)] bg-[var(--paper)] p-3"
    >
      <div class="flex items-center gap-2 text-xs font-semibold">
        <AudioLines size={14} class="text-[var(--accent)]" /> Select up to three audio
        witnesses
      </div>
      <p class="muted mt-1 text-[.62rem] leading-relaxed">
        Only active models explicitly configured with audio input appear here.
        That declaration is not treated as proof: each result records the wire
        format and whether the provider reported consuming audio tokens.
      </p>
      {#if loadingAudioModels}
        <p class="muted mt-2 flex items-center gap-2 text-xs">
          <LoaderCircle size={13} class="animate-spin" /> Loading configured models…
        </p>
      {:else if audioModelsError}
        <p class="mt-2 text-xs text-red-600">{audioModelsError}</p>
      {:else if !audioModels.length}
        <p class="mt-2 text-xs text-amber-700">
          No active model is marked for audio input. Add that capability in
          Provider settings after verifying the model and endpoint contract.
        </p>
      {:else}
        <div class="mt-2 grid gap-2 sm:grid-cols-2">
          {#each audioModels as model (model.id)}
            <label
              class:selected={selectedAudioModelIds.includes(model.id)}
              class="route-option"
            >
              <input
                type="checkbox"
                checked={selectedAudioModelIds.includes(model.id)}
                disabled={!selectedAudioModelIds.includes(model.id) &&
                  selectedAudioModelIds.length >= 3}
                onchange={() => toggleAudioModel(model.id)}
              />
              <span class="min-w-0">
                <strong>{model.modelId}</strong>
                <small
                  >{model.providerLabel}{model.custom
                    ? ' · OpenAI-compatible transport'
                    : ''}</small
                >
              </span>
            </label>
          {/each}
        </div>
      {/if}
    </div>
  {/if}

  <div class="mt-3 flex flex-wrap gap-2">
    <button
      onclick={requestEvidence}
      disabled={requesting ||
        active ||
        !segment.id ||
        !selectedRoutes.length ||
        !reason.trim() ||
        !audioReady()}
      class="primary-action"
    >
      {#if requesting || active}<LoaderCircle
          size={13}
          class="animate-spin"
        />{:else}<RotateCcw size={13} />{/if}
      {latestRecord ? 'Escalate and retry' : 'Request re-transcription'}
    </button>
    <button
      onclick={() => resolve('uncertain')}
      disabled={resolving || active || !reason.trim()}
      class="quiet-action"
    >
      <CircleHelp size={13} /> Mark uncertain
    </button>
    <button
      onclick={() => resolve('deleted')}
      disabled={resolving || active}
      class="delete-action"
    >
      <Trash2 size={13} /> Delete cue
    </button>
  </div>

  {#if error}<p
      class="mt-3 rounded-lg bg-red-500/10 px-3 py-2 text-xs text-red-600"
    >
      {error}
    </p>{/if}

  {#if latestRecord?.clip_artifact_id}
    <div class="mt-3">
      <AudioPlayer
        compact
        src={`/api/v1/artifacts/${latestRecord.clip_artifact_id}/content`}
        label="Bounded evidence clip"
      />
      <p class="muted mt-1 text-[.62rem] tabular-nums">
        Clip {clock(latestRecord.clip_start_ms)}–{clock(
          latestRecord.clip_end_ms
        )} in the source
      </p>
    </div>
  {/if}

  {#if candidates.length}
    <div class="mt-3 space-y-2" aria-label="Transcription candidates">
      {#each candidates as candidate (candidate.id)}
        <article class:failed={candidate.status === 'failed'} class="candidate">
          <div class="flex flex-wrap items-center justify-between gap-2">
            <strong class="text-xs">{routeLabel(candidate.route)}</strong>
            <span class="muted text-[.62rem]">
              {candidate.timing_kind?.replaceAll('_', ' ') ??
                'timing unavailable'}
              {#if confidence(candidate)}
                · {confidence(candidate)}{/if}
            </span>
          </div>
          {#if candidate.status === 'success'}
            <p
              class="mt-2 text-[.62rem] font-semibold uppercase tracking-wide text-[var(--muted)]"
            >
              Suggested cue text
            </p>
            <p
              class="mt-1 whitespace-pre-wrap text-sm font-medium leading-relaxed"
            >
              {candidate.text}
            </p>
            {#if candidate.context_text && candidate.context_text !== candidate.text}
              <details
                class="mt-2 rounded-lg border border-[var(--line)] px-2 py-1.5"
              >
                <summary
                  class="muted cursor-pointer text-[.62rem] font-semibold"
                  >Full context transcript</summary
                >
                <p class="mt-1 whitespace-pre-wrap text-xs leading-relaxed">
                  {candidate.context_text}
                </p>
              </details>
            {/if}
            <div class="muted mt-2 flex flex-wrap gap-x-3 text-[.62rem]">
              <span
                >{candidate.model || candidate.engine || candidate.route}</span
              >
              <span>{costLabel(candidate)}</span>
            </div>
            {#if transportLabel(candidate)}
              <p
                class:warning-copy={candidate.transport?.audio_consumption !==
                  'confirmed'}
                class="muted mt-1 text-[.62rem]"
              >
                {transportLabel(candidate)}
              </p>
            {/if}
            <button
              onclick={() => resolve('accepted', candidate)}
              disabled={resolving}
              class="primary-action mt-2"
              ><Check size={13} /> Use this transcript</button
            >
          {:else}
            <p class="mt-2 flex items-start gap-2 text-xs text-amber-700">
              <AlertTriangle size={14} class="mt-0.5 shrink-0" />
              {candidate.error ||
                'This route did not return usable timed speech.'}
            </p>
          {/if}
        </article>
      {/each}
    </div>
  {:else if latestRecord?.status === 'failed'}
    <p
      class="mt-3 flex items-start gap-2 rounded-lg bg-amber-500/10 px-3 py-2 text-xs text-amber-800"
    >
      <AlertTriangle size={14} class="mt-0.5 shrink-0" />
      {latestRecord.error_message ||
        'No route returned usable evidence. Mark the cue uncertain or edit/delete it manually.'}
    </p>
  {/if}

  <details class="mt-3 border-t border-[var(--line)] pt-2">
    <summary class="muted cursor-pointer text-[.65rem] font-semibold"
      >How audio is sent</summary
    >
    <p class="muted mt-2 text-[.65rem] leading-relaxed">
      Pandrator sends raw Base64 WAV in the OpenAI <code>input_audio</code>
      block. The Gemini and Vertex adapters convert that to Google
      <code>inlineData</code>; custom OpenAI-compatible endpoints receive
      <code>input_audio</code> unchanged. OpenCode Go currently declares audio for
      some models but has not documented a reliable wire contract, so missing audio-token
      accounting remains visibly unverified rather than being promoted to trusted
      evidence.
    </p>
  </details>
</section>

<style>
  .route-option {
    display: flex;
    cursor: pointer;
    gap: 0.55rem;
    border: 1px solid var(--line);
    border-radius: 0.7rem;
    background: var(--paper);
    padding: 0.6rem;
  }
  .route-option.selected {
    border-color: color-mix(in srgb, var(--accent) 55%, var(--line));
    background: var(--accent-soft);
  }
  .route-option input {
    margin-top: 0.1rem;
    accent-color: var(--accent);
  }
  .route-option strong,
  .route-option small {
    display: block;
  }
  .route-option strong {
    font-size: 0.72rem;
  }
  .route-option small {
    margin-top: 0.15rem;
    color: var(--muted);
    font-size: 0.62rem;
    line-height: 1.35;
  }
  .commercial,
  .status {
    display: inline-flex;
    align-items: center;
    gap: 0.2rem;
    margin-left: 0.35rem;
    border-radius: 999px;
    background: var(--accent-soft);
    padding: 0.1rem 0.35rem;
    color: var(--accent);
    font-size: 0.55rem;
    font-weight: 750;
    text-transform: uppercase;
  }
  .status.warning {
    background: #f59e0b1a;
    color: #a16207;
  }
  .status.running {
    background: #2563eb1a;
    color: #2563eb;
  }
  .candidate {
    border: 1px solid var(--line);
    border-radius: 0.75rem;
    background: var(--paper);
    padding: 0.7rem;
  }
  .candidate.failed {
    border-color: color-mix(in srgb, #f59e0b 35%, var(--line));
  }
  .warning-copy {
    color: #a16207;
  }
  .primary-action,
  .quiet-action,
  .delete-action {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    border-radius: 0.55rem;
    padding: 0.38rem 0.6rem;
    font-size: 0.68rem;
    font-weight: 700;
  }
  .primary-action {
    background: var(--accent);
    color: white;
  }
  .quiet-action {
    border: 1px solid var(--line);
  }
  .delete-action {
    border: 1px solid color-mix(in srgb, #ef4444 45%, var(--line));
    color: #dc2626;
  }
  button:disabled {
    cursor: not-allowed;
    opacity: 0.4;
  }
</style>
