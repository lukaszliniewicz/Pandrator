<script lang="ts">
  import {
    Check,
    ChevronDown,
    ChevronRight,
    CircleAlert,
    Clock3,
    Eye,
    LoaderCircle,
    Play,
    Scissors,
    Settings2,
    Sparkles
  } from '@lucide/svelte';
  import type { WorkflowStage } from './api-models';
  import { modelDisplayName } from './model-display';
  import StageArtifactHistory from './StageArtifactHistory.svelte';
  import type { Snippet } from 'svelte';
  import type { StageArtifact } from './stage-artifacts';

  let {
    stage,
    workspaceMode,
    optional = false,
    runLabel = '',
    historyLoading = false,
    runDisabled = false,
    outputsOnly = false,
    onpreviewversion,
    inputControls,
    onsettings,
    ontoggle,
    onrun,
    onresume,
    oncancel,
    onselect,
    onpreview,
    onclear,
    onfork,
    ondelete,
    onloadmore
  }: {
    stage: WorkflowStage;
    workspaceMode: 'review' | 'automatic';
    optional?: boolean;
    runLabel?: string;
    historyLoading?: boolean;
    runDisabled?: boolean;
    outputsOnly?: boolean;
    onpreviewversion?: (artifact: StageArtifact) => void;
    inputControls?: Snippet;
    onsettings: () => void;
    ontoggle: (enabled: boolean) => void;
    onrun: () => void;
    onresume: () => void;
    oncancel: () => void;
    onselect: (artifactId: string) => void;
    onpreview: () => void;
    onclear: () => void;
    onfork?: () => void;
    ondelete: (artifact: StageArtifact) => void;
    onloadmore: () => void;
  } = $props();

  const statusIcon = (status: WorkflowStage['status']) => {
    if (status === 'completed') return Check;
    if (status === 'running') return LoaderCircle;
    if (status === 'stale' || status === 'failed') return CircleAlert;
    return Clock3;
  };

  const progressPercent = (value: number | null | undefined) => {
    const numeric = Number(value ?? 0);
    return Math.round(
      Math.max(0, Math.min(1, Number.isFinite(numeric) ? numeric : 0)) * 100
    );
  };

  const formatCost = (cost: number | null) => {
    if (cost == null) return 'not metered';
    if (cost === 0) return '$0.00';
    return cost < 0.01 ? `$${cost.toFixed(6)}` : `$${cost.toFixed(4)}`;
  };

  const formatDuration = (seconds: number) => {
    const rounded = Math.max(0, Math.round(seconds));
    if (rounded < 60) return `${rounded}s`;
    const minutes = Math.floor(rounded / 60);
    const remainingSeconds = rounded % 60;
    if (minutes < 60)
      return remainingSeconds
        ? `${minutes}m ${remainingSeconds}s`
        : `${minutes}m`;
    const hours = Math.floor(minutes / 60);
    const remainingMinutes = minutes % 60;
    return remainingMinutes ? `${hours}h ${remainingMinutes}m` : `${hours}h`;
  };

  let nowMs = $state(Date.now());
  $effect(() => {
    if (!stage.run_metrics?.started_at || stage.run_metrics.finished_at) return;
    nowMs = Date.now();
    const timer = window.setInterval(() => (nowMs = Date.now()), 1000);
    return () => window.clearInterval(timer);
  });
  const elapsedSeconds = $derived.by(() => {
    const metrics = stage.run_metrics;
    if (!metrics?.started_at) return metrics?.duration_seconds ?? null;
    if (metrics.finished_at) return metrics.duration_seconds;
    const started = Date.parse(metrics.started_at);
    return Number.isFinite(started)
      ? Math.max(0, (nowMs - started) / 1000)
      : metrics.duration_seconds;
  });
  const costAware = $derived(
    ['correct', 'translate', 'optimize_tts', 'generate_audio'].includes(
      stage.key
    )
  );
  const metadataNumber = (
    metadata: Record<string, unknown>,
    ...keys: string[]
  ) => {
    for (const key of keys) {
      const value = Number(metadata[key]);
      if (Number.isFinite(value)) return value;
    }
    return 0;
  };
  const captionAlignment = $derived.by(() => {
    const metadata = stage.artifact?.metadata_json;
    if (!metadata) return null;
    const method = String(metadata.alignment_method ?? '');
    if (
      ![
        'asr_lexical_projection',
        'ctc_cue_alignment',
        'ctc_with_asr_fallback'
      ].includes(method)
    )
      return null;
    const coverage = Math.max(
      0,
      Math.min(1, metadataNumber(metadata, 'alignment_coverage', 'coverage'))
    );
    const eligibleCoverage = Math.max(
      0,
      Math.min(
        1,
        metadataNumber(
          metadata,
          'alignment_eligible_coverage',
          'eligible_alignment_coverage',
          'eligible_coverage',
          'alignment_coverage',
          'coverage'
        )
      )
    );
    const quality = Math.max(
      0,
      Math.min(
        1,
        metadataNumber(
          metadata,
          'alignment_quality',
          'alignment_confidence',
          'confidence'
        )
      )
    );
    const ctc = method !== 'asr_lexical_projection';
    return {
      method,
      methodLabel:
        method === 'ctc_cue_alignment'
          ? 'Cue-local CTC'
          : method === 'ctc_with_asr_fallback'
            ? 'Cue-local CTC + ASR fallback'
            : 'ASR lexical projection',
      coverage,
      eligibleCoverage,
      quality,
      qualityLabel: ctc ? 'mean timing quality' : 'mean cue confidence',
      wordCount: Math.max(
        0,
        metadataNumber(metadata, 'word_count', 'accepted_token_count')
      ),
      acceptedCues: Math.max(
        0,
        metadataNumber(metadata, 'accepted_cue_count', 'aligned_cue_count')
      ),
      cueCount: Math.max(
        0,
        metadataNumber(metadata, 'cue_count', 'total_cue_count')
      ),
      batchCount: Math.max(
        0,
        metadataNumber(metadata, 'batch_count', 'first_pass_batch_count')
      ),
      retryCount: Math.max(
        0,
        metadataNumber(metadata, 'isolated_retry_count', 'retry_count') ||
          metadataNumber(metadata, 'cluster_retries') +
            metadataNumber(metadata, 'individual_retries')
      ),
      outsideMediaCount: Math.max(
        0,
        metadataNumber(
          metadata,
          'outside_media_cue_count',
          'outside_media_count'
        )
      ),
      oversizedCueCount: Math.max(
        0,
        metadataNumber(metadata, 'oversized_cue_count')
      ),
      engine: String(
        metadata.ctc_model ??
          metadata.ctc_engine ??
          metadata.engine ??
          metadata.model ??
          ''
      ).trim(),
      fallbackUsed:
        metadata.fallback_used === true || metadata.fallback_triggered === true,
      reliable: coverage >= 0.5 && eligibleCoverage >= 0.5
    };
  });

  const StatusIcon = $derived(statusIcon(stage.status));
</script>

<article
  class:stage-locked={stage.status === 'unavailable'}
  class:stage-disabled={Boolean(stage.toggle && !stage.enabled)}
  class="surface rounded-[1.4rem] p-5 sm:p-6"
>
  <div class="grid gap-5">
    <div class="flex min-w-0 flex-1 items-start gap-4">
      <div
        class="grid size-11 shrink-0 place-items-center rounded-2xl bg-[var(--accent-soft)] text-sm font-bold text-[var(--accent)]"
      >
        {stage.number}
      </div>
      <div class="min-w-0 flex-1">
        <div class="flex flex-wrap items-center gap-2">
          <h2 class="text-lg font-semibold">{stage.title}</h2>
          <span
            class:running={stage.status === 'running'}
            class:done={stage.status === 'completed'}
            class:warning={stage.status === 'stale' ||
              stage.status === 'failed'}
            class="status-chip inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[.68rem] font-bold uppercase tracking-wider"
          >
            <StatusIcon
              class={stage.status === 'running' ? 'animate-spin' : ''}
              size={12}
            />
            {stage.toggle
              ? stage.enabled
                ? 'enabled'
                : 'disabled'
              : optional && stage.status === 'ready'
                ? 'optional'
                : stage.status}
          </span>
        </div>
        <p class="muted mt-1.5 max-w-3xl text-sm leading-relaxed">
          {stage.explanation}
        </p>

        {#if captionAlignment}
          <details
            class:alignment-warning={!captionAlignment.reliable}
            class="alignment-result mt-3 max-w-3xl overflow-hidden rounded-xl border text-sm"
          >
            <summary
              class="alignment-summary flex cursor-pointer list-none items-center gap-2 px-3.5 py-3"
            >
              {#if captionAlignment.reliable}<Check
                  size={15}
                />{:else}<CircleAlert size={15} />{/if}
              <span class="min-w-0 flex-1">
                <strong class="block">
                  {captionAlignment.reliable
                    ? 'Caption word timing is ready'
                    : 'Caption alignment is unreliable'}
                </strong>
                <span class="muted mt-0.5 block text-xs font-normal">
                  {captionAlignment.methodLabel} · {Math.round(
                    captionAlignment.eligibleCoverage * 100
                  )}% of in-media words · {Math.round(
                    captionAlignment.quality * 100
                  )}% {captionAlignment.qualityLabel}
                </span>
              </span>
              <span class="alignment-chevron muted shrink-0"
                ><ChevronDown size={16} /></span
              >
            </summary>
            <div class="border-t border-[var(--line)] px-3.5 py-3">
              <p class="text-xs font-semibold leading-relaxed">
                {captionAlignment.reliable
                  ? 'No action is required. The editor uses these acoustic word times to inspect and refine cuts while preserving the caption wording and speakers.'
                  : 'Review the diagnostics before relying on automatic cut refinement.'}
              </p>
              {#if captionAlignment.reliable}
                <p class="muted mt-2 text-xs leading-relaxed">
                  After rendering, subtitle formatting and speech-block creation
                  currently use the retimed SRT. The word-timing artifact stays
                  attached for provenance, but those later stages do not consume
                  it directly yet.
                </p>
              {/if}
              <p class="muted mt-2 text-xs leading-relaxed">
                {captionAlignment.methodLabel} · {Math.round(
                  captionAlignment.coverage * 100
                )}% of all caption words aligned · {Math.round(
                  captionAlignment.eligibleCoverage * 100
                )}% of in-media words · {Math.round(
                  captionAlignment.quality * 100
                )}% {captionAlignment.qualityLabel}{captionAlignment.wordCount
                  ? ` · ${captionAlignment.wordCount.toLocaleString()} timed words`
                  : ''}{captionAlignment.cueCount
                  ? ` · ${captionAlignment.acceptedCues.toLocaleString()}/${captionAlignment.cueCount.toLocaleString()} cues accepted`
                  : ''}{captionAlignment.batchCount
                  ? ` · ${captionAlignment.batchCount.toLocaleString()} CTC batches`
                  : ''}{captionAlignment.retryCount
                  ? ` · ${captionAlignment.retryCount.toLocaleString()} isolated retries`
                  : ''}{captionAlignment.outsideMediaCount
                  ? ` · ${captionAlignment.outsideMediaCount.toLocaleString()} cues outside the recording`
                  : ''}{captionAlignment.oversizedCueCount
                  ? ` · ${captionAlignment.oversizedCueCount.toLocaleString()} oversized cues retained without CTC timing`
                  : ''}{captionAlignment.engine
                  ? ` · ${captionAlignment.engine}`
                  : ''}{captionAlignment.fallbackUsed
                  ? ' · ASR fallback used'
                  : ''}.
                {captionAlignment.reliable
                  ? ' The timing is stored with the caption transcript; rebuild an existing edit timeline to consume it.'
                  : ' Original caption timing was retained for rejected cues; those word times are excluded from cut refinement.'}
              </p>
            </div>
          </details>
        {/if}

        {#if stage.key === 'generate_audio' && stage.resolved_input && !inputControls}
          <div
            class="mt-3 max-w-3xl rounded-xl border border-[var(--line)] bg-[var(--paper-strong)] px-3.5 py-3 text-sm"
            aria-label={`Generation input: ${stage.resolved_input.label}${stage.resolved_input.version ? ` v${stage.resolved_input.version}` : ''}`}
          >
            <strong
              >Generate from: {stage.resolved_input.label}{stage.resolved_input
                .version
                ? ` v${stage.resolved_input.version}`
                : ''}</strong
            >
            <p class="muted mt-1 text-xs leading-relaxed">
              Change the input role in Customize workflow. Choose the exact
              version with that stage's Selected version control.
            </p>
          </div>
        {/if}

        {#if stage.status === 'running' && stage.progress != null}
          {@const percent = progressPercent(stage.progress)}
          <div class="mt-3 max-w-md">
            <div class="flex items-center justify-between gap-3 text-xs">
              <span class="muted min-w-0 truncate" title={stage.detail ?? ''}
                >{stage.detail ?? 'Working…'}</span
              >
              <span class="muted shrink-0 tabular-nums">{percent}%</span>
            </div>
            <div
              class="mt-1.5 h-1.5 overflow-hidden rounded-full bg-[var(--line)]"
              role="progressbar"
              aria-label={`${stage.title} progress`}
              aria-valuemin="0"
              aria-valuemax="100"
              aria-valuenow={percent}
            >
              <div
                class="h-full bg-[var(--accent)] transition-[width]"
                style={`width:${percent}%`}
              ></div>
            </div>
          </div>
        {/if}

        {#if stage.detail && stage.status === 'failed'}
          <p class="mt-2 text-xs text-red-500">{stage.detail}</p>
        {/if}

        {#if elapsedSeconds != null || stage.usage || (costAware && stage.run_metrics)}
          <div
            class="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs"
            aria-label={`${stage.title} run metrics`}
          >
            {#if elapsedSeconds != null}
              <span class="muted inline-flex items-center gap-1.5 tabular-nums">
                <Clock3 size={13} aria-hidden="true" />
                {stage.status === 'running' ? 'Elapsed' : 'Duration'}
                <strong class="text-[var(--ink)]"
                  >{formatDuration(elapsedSeconds)}</strong
                >
              </span>
            {/if}
            {#if stage.usage || (costAware && stage.run_metrics)}
              <span
                class="muted"
                title={stage.usage
                  ? `${stage.usage.total_tokens.toLocaleString()} tokens (${stage.usage.input_tokens.toLocaleString()} input, ${stage.usage.output_tokens.toLocaleString()} output${stage.usage.cached_input_tokens ? `, ${stage.usage.cached_input_tokens.toLocaleString()} cached` : ''})`
                  : 'This run did not report metered provider usage.'}
              >
                Cost
                <strong class="text-[var(--ink)]"
                  >{formatCost(stage.usage?.cost_usd ?? null)}</strong
                >
              </span>
              {#if stage.usage}
                <span class="muted tabular-nums">
                  Input
                  <strong class="text-[var(--ink)]"
                    >{stage.usage.input_tokens.toLocaleString()}</strong
                  >
                </span>
                <span class="muted tabular-nums">
                  Output
                  <strong class="text-[var(--ink)]"
                    >{stage.usage.output_tokens.toLocaleString()}</strong
                  >
                </span>
                {#if stage.usage.cached_input_tokens}
                  <span class="muted tabular-nums">
                    Cached
                    <strong class="text-[var(--ink)]"
                      >{stage.usage.cached_input_tokens.toLocaleString()}</strong
                    >
                  </span>
                {/if}
              {/if}
              {#if stage.usage?.model_id}
                <span
                  class="muted max-w-full truncate"
                  title={stage.usage.model_id}
                  aria-label={`Model ${modelDisplayName(stage.usage.model_id)}`}
                  >{modelDisplayName(stage.usage.model_id)}</span
                >
              {/if}
            {/if}
          </div>
        {/if}

        {#if inputControls}<div class="my-3">
            {@render inputControls()}
          </div>{/if}
        {#if (stage.artifacts?.length ?? 0) > 0}
          <StageArtifactHistory
            {outputsOnly}
            {onpreviewversion}
            artifacts={stage.artifacts ?? []}
            total={stage.artifact_history_total}
            hasMore={Boolean(stage.artifact_history_has_more)}
            loadingMore={historyLoading}
            selectedArtifactId={stage.selected_artifact_id}
            canPreview={Boolean(stage.artifact)}
            {onselect}
            {onpreview}
            {onclear}
            {onfork}
            {ondelete}
            {onloadmore}
          />
        {:else if stage.artifact}
          <button
            onclick={onpreview}
            class="mt-2 flex items-center gap-1 text-xs font-semibold text-[var(--accent)]"
          >
            Preview latest: {stage.artifact.role}<ChevronRight size={13} />
          </button>
        {/if}
      </div>
    </div>

    <div class="flex flex-wrap items-center gap-2 sm:ml-[3.75rem]">
      {#if stage.toggle}
        <button
          onclick={onsettings}
          class="flex items-center gap-2 rounded-xl border border-[var(--line)] px-3.5 py-2.5 text-sm font-semibold"
        >
          <Settings2 size={16} /> Timing &amp; settings
        </button>
        <label
          class="flex cursor-pointer items-center gap-3 rounded-xl border border-[var(--line)] px-3.5 py-2.5 text-sm font-semibold"
        >
          <input
            type="checkbox"
            checked={Boolean(stage.enabled)}
            onchange={(event) => ontoggle(event.currentTarget.checked)}
            class="size-4 accent-[var(--accent)]"
          />
          {stage.enabled ? 'Enabled' : 'Disabled'}
        </label>
        {#if !stage.toggle_only && stage.enabled}
          {#if stage.status === 'running'}
            <button
              onclick={oncancel}
              class="rounded-xl border border-red-400/50 px-4 py-2.5 text-sm font-semibold text-red-500"
              >Cancel</button
            >
          {:else}
            <button
              onclick={stage.status === 'failed' &&
              stage.agent_run_id &&
              stage.resumable
                ? onresume
                : onrun}
              disabled={runDisabled || stage.status === 'unavailable'}
              class="flex items-center gap-2 rounded-xl bg-[var(--accent)] px-4 py-2.5 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:opacity-35"
            >
              <Play size={16} />
              {stage.status === 'failed' &&
              stage.agent_run_id &&
              stage.resumable
                ? 'Resume'
                : stage.status === 'failed'
                  ? 'Retry optimization'
                  : 'Run optimization'}
            </button>
          {/if}
        {/if}
      {:else if stage.executable}
        <button
          onclick={onsettings}
          class="flex items-center gap-2 rounded-xl border border-[var(--line)] px-3.5 py-2.5 text-sm font-semibold"
        >
          <Settings2 size={16} /> Settings
        </button>
        {#if stage.key === 'export' && stage.artifact}
          <button
            onclick={onpreview}
            class="flex items-center gap-2 rounded-xl border border-[var(--line)] px-3.5 py-2.5 text-sm font-semibold"
          >
            <Eye size={16} /> Preview latest
          </button>
        {/if}
        {#if workspaceMode === 'review' || stage.key === 'export' || stage.status === 'failed'}
          {#if stage.status === 'running'}
            <button
              onclick={oncancel}
              class="rounded-xl border border-red-400/50 px-4 py-2.5 text-sm font-semibold text-red-500"
              >Cancel</button
            >
          {:else}
            <button
              onclick={stage.status === 'failed' &&
              stage.agent_run_id &&
              stage.resumable
                ? onresume
                : onrun}
              disabled={runDisabled || stage.status === 'unavailable'}
              class="flex items-center gap-2 rounded-xl bg-[var(--accent)] px-4 py-2.5 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:opacity-35"
            >
              <Play size={16} />
              {stage.key === 'export'
                ? 'Export now'
                : stage.status === 'failed' &&
                    stage.agent_run_id &&
                    stage.resumable
                  ? 'Resume'
                  : stage.status === 'failed'
                    ? 'Retry'
                    : runLabel
                      ? runLabel
                      : optional && stage.key === 'transcribe'
                        ? 'Configure transcription'
                        : stage.artifact
                          ? 'Run again'
                          : 'Run now'}
            </button>
          {/if}
        {/if}
      {:else}
        <button
          onclick={onrun}
          disabled={runDisabled || stage.status === 'unavailable'}
          class="flex items-center gap-2 rounded-xl bg-[var(--accent)] px-4 py-2.5 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:opacity-35"
        >
          {#if stage.key === 'edit_media'}
            <Scissors size={16} /> Open editor
          {:else}
            <Sparkles size={16} /> Open comparison
          {/if}
        </button>
      {/if}
    </div>
  </div>
</article>

<style>
  .status-chip {
    color: var(--muted);
    background: color-mix(in srgb, var(--muted) 10%, transparent);
  }
  .status-chip.done {
    color: var(--success);
    background: color-mix(in srgb, var(--success) 12%, transparent);
  }
  .status-chip.running {
    color: var(--accent);
    background: var(--accent-soft);
  }
  .status-chip.warning {
    color: var(--warning);
    background: color-mix(in srgb, var(--warning) 12%, transparent);
  }
  .stage-locked {
    opacity: 0.58;
  }
  .stage-disabled {
    background: color-mix(in srgb, var(--paper-strong) 82%, var(--muted) 18%);
    box-shadow: none;
    filter: saturate(0.45);
    opacity: 0.72;
  }
  .alignment-result {
    border-color: color-mix(in srgb, var(--success) 35%, var(--line));
    background: color-mix(in srgb, var(--success) 8%, transparent);
  }
  .alignment-result.alignment-warning {
    border-color: color-mix(in srgb, var(--warning) 45%, var(--line));
    background: color-mix(in srgb, var(--warning) 10%, transparent);
  }
  .alignment-summary::-webkit-details-marker {
    display: none;
  }
  .alignment-chevron {
    transition: transform 160ms ease;
  }
  details[open] .alignment-chevron {
    transform: rotate(180deg);
  }
</style>
