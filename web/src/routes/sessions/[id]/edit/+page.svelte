<script lang="ts">
  import { page } from '$app/state';
  import {
    Bot,
    Check,
    ChevronDown,
    ChevronRight,
    CircleAlert,
    Clock3,
    Download,
    LoaderCircle,
    LocateFixed,
    Play,
    Plus,
    RefreshCw,
    Save,
    Scissors,
    Search,
    Settings2,
    SkipBack,
    SkipForward,
    Sparkles,
    X,
    Trash2
  } from '@lucide/svelte';
  import { onDestroy, onMount } from 'svelte';
  import {
    appApi,
    artifactApi,
    jobApi,
    mediaEditApi,
    sessionApi
  } from '$lib/domain-api';
  import type {
    JobRecord,
    MediaEditDispatchRun,
    MediaEditPlan,
    MediaEditRange,
    MediaEditState,
    RuntimeCapabilities,
    SettingsPayload
  } from '$lib/api-models';
  import { errorMessage } from '$lib/errors';
  import MediaTimeline from '$lib/MediaTimeline.svelte';
  import { modalFocus } from '$lib/modal-focus';

  type CutRange = MediaEditRange;
  type CutEdge = 'start_ms' | 'end_ms';

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
  let proposalMode = $state<'passive' | 'configured'>('passive');
  let proposalModel = $state('default');
  let proposalModels = $state<
    { value: string; label: string; isDefault: boolean }[]
  >([]);
  let proposalModelsLoading = $state(false);
  let proposalModelsError = $state('');
  let passiveRun = $state<MediaEditDispatchRun | null>(null);
  let passivePollTimer: number | undefined;
  let renderOpen = $state(false);
  let renderSettings = $state<SettingsPayload | null>(null);
  let renderCapabilities = $state<RuntimeCapabilities>({});
  let renderSettingsLoading = $state(false);
  let renderSettingsError = $state('');
  let renderDraft = $state<Record<string, unknown>>({});

  const passiveActive = $derived(
    passiveRun != null &&
      ['ready', 'running', 'finalizing'].includes(passiveRun.status)
  );
  const renderEncoderOptions = $derived(
    renderCapabilities.ffmpeg?.burn_video_encoders?.length
      ? renderCapabilities.ffmpeg.burn_video_encoders
      : [
          {
            id: 'libx264',
            label: 'H.264 software (most compatible)',
            hardware: false,
            codec: 'h264'
          }
        ]
  );
  const verifiedHardwareEncoderAvailable = $derived(
    renderEncoderOptions.some((item) => item.hardware)
  );

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
  const editPoints = $derived.by(() =>
    cuts
      .flatMap((range) => [
        {
          rangeId: range.id,
          edge: 'start_ms' as const,
          timeMs: range.start_ms
        },
        { rangeId: range.id, edge: 'end_ms' as const, timeMs: range.end_ms }
      ])
      .sort((a, b) => a.timeMs - b.timeMs)
  );
  function evidenceNumber(
    evidence: Record<string, unknown>,
    ...keys: string[]
  ) {
    for (const key of keys) {
      const value = Number(evidence[key]);
      if (Number.isFinite(value)) return value;
    }
    return 0;
  }

  const alignmentSummary = $derived.by(() => {
    if (!plan || typeof plan.evidence.alignment_coverage !== 'number')
      return null;
    const evidence = plan.evidence;
    const counts =
      evidence.alignment_counts && typeof evidence.alignment_counts === 'object'
        ? (evidence.alignment_counts as Record<string, unknown>)
        : {};
    const method = String(evidence.alignment_method ?? '');
    const coverage = Math.max(
      0,
      Math.min(1, evidenceNumber(evidence, 'alignment_coverage', 'coverage'))
    );
    const eligibleCoverage = Math.max(
      0,
      Math.min(
        1,
        evidenceNumber(
          evidence,
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
        evidenceNumber(
          evidence,
          'alignment_quality',
          'alignment_confidence',
          'confidence'
        )
      )
    );
    const ctc =
      method === 'ctc_cue_alignment' || method === 'ctc_with_asr_fallback';
    return {
      methodLabel:
        method === 'ctc_cue_alignment'
          ? 'Cue-local CTC'
          : method === 'ctc_with_asr_fallback'
            ? 'Cue-local CTC + ASR fallback'
            : method === 'asr_lexical_projection'
              ? 'ASR lexical projection'
              : 'Caption alignment',
      coverage,
      eligibleCoverage,
      quality,
      qualityLabel: ctc ? 'mean timing quality' : 'mean cue confidence',
      wordCount: Math.max(
        0,
        evidenceNumber(evidence, 'word_count', 'accepted_token_count') ||
          evidenceNumber(counts, 'accepted_token_count')
      ),
      acceptedCues: Math.max(
        0,
        evidenceNumber(evidence, 'accepted_cue_count', 'aligned_cue_count') ||
          evidenceNumber(counts, 'accepted_cue_count')
      ),
      cueCount: Math.max(
        0,
        evidenceNumber(evidence, 'cue_count', 'total_cue_count')
      ),
      batchCount: Math.max(
        0,
        evidenceNumber(evidence, 'batch_count', 'first_pass_batch_count') ||
          evidenceNumber(counts, 'first_pass_batch_count')
      ),
      retryCount: Math.max(
        0,
        evidenceNumber(evidence, 'isolated_retry_count', 'retry_count') ||
          evidenceNumber(evidence, 'cluster_retries') +
            evidenceNumber(evidence, 'individual_retries') ||
          evidenceNumber(counts, 'cluster_retries') +
            evidenceNumber(counts, 'individual_retries')
      ),
      outsideMediaCount: Math.max(
        0,
        evidenceNumber(
          evidence,
          'outside_media_cue_count',
          'outside_media_count'
        ) || evidenceNumber(counts, 'outside_media_count')
      ),
      oversizedCueCount: Math.max(
        0,
        evidenceNumber(evidence, 'oversized_cue_count') ||
          evidenceNumber(counts, 'oversized_cue_count')
      ),
      fallbackUsed:
        evidence.fallback_used === true || evidence.fallback_triggered === true,
      reliable: coverage >= 0.5 && eligibleCoverage >= 0.5
    };
  });

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
      void loadLatestPassiveRun();
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
      const selected =
        proposalModels.find((item) => item.isDefault) ?? proposalModels[0];
      proposalModel = selected?.value ?? 'default';
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
        if (current.status === 'succeeded') {
          await load();
          message = successMessage;
        } else {
          error = current.error_message || `The ${current.kind} job failed.`;
        }
      } catch (caught) {
        error = errorMessage(caught);
      } finally {
        if (
          activeJob &&
          !['queued', 'running', 'cancel_requested'].includes(activeJob.status)
        )
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

  function schedulePassivePoll() {
    if (passivePollTimer !== undefined) window.clearTimeout(passivePollTimer);
    passivePollTimer = undefined;
    if (!passiveActive || !passiveRun) return;
    passivePollTimer = window.setTimeout(() => void pollPassiveRun(), 1200);
  }

  async function pollPassiveRun() {
    if (!passiveRun) return;
    try {
      const current = await mediaEditApi.dispatchRun(passiveRun.id);
      passiveRun = current;
      if (current.status === 'completed') {
        await load();
        message =
          'The passive proposal is ready. Review every red range before rendering.';
      } else if (current.status === 'failed') {
        error = current.error_message || 'The passive media-edit task failed.';
      }
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      schedulePassivePoll();
    }
  }

  async function loadLatestPassiveRun() {
    try {
      const payload = await mediaEditApi.dispatchRuns(sessionId, 20);
      const active = payload.items.find((item) =>
        ['ready', 'running', 'finalizing'].includes(item.status)
      );
      passiveRun = active ?? payload.items[0] ?? null;
      schedulePassivePoll();
    } catch {
      // The editable plan remains usable if passive-run status is unavailable.
    }
  }

  async function createPassiveProposal() {
    if (!plan || !instructions.trim()) return;
    error = '';
    message = '';
    const saved = await save(false);
    if (!saved) return;
    busy = 'passive';
    try {
      passiveRun = await mediaEditApi.createDispatch(
        sessionId,
        saved.revision,
        instructions
      );
      proposalOpen = false;
      message =
        'Passive task created. A connected MCP agent can now claim the transcript and submit a proposal.';
      schedulePassivePoll();
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      busy = '';
    }
  }

  function renderSetting(key: string, fallback: unknown) {
    if (Object.prototype.hasOwnProperty.call(renderDraft, key))
      return renderDraft[key];
    return renderSettings?.effective?.[key] ?? fallback;
  }

  function setRenderSetting(key: string, value: unknown) {
    renderDraft = { ...renderDraft, [key]: value };
  }

  async function openRender() {
    renderOpen = true;
    renderSettingsLoading = true;
    renderSettingsError = '';
    try {
      [renderSettings, renderCapabilities] = await Promise.all([
        sessionApi.settings(sessionId, 'output'),
        appApi.capabilities(true)
      ]);
      const availableIds = new Set(
        renderCapabilities.ffmpeg?.burn_video_encoders?.map(
          (item) => item.id
        ) ?? ['libx264']
      );
      const currentEncoder = String(
        renderSettings.effective?.burn_video_encoder ?? 'libx264'
      );
      renderDraft = {
        burn_video_encoder: availableIds.has(currentEncoder)
          ? currentEncoder
          : 'libx264',
        burn_video_resolution: String(
          renderSettings.effective?.burn_video_resolution ?? 'source'
        ),
        burn_video_quality: Number(
          renderSettings.effective?.burn_video_quality ?? 18
        ),
        burn_video_speed: String(
          renderSettings.effective?.burn_video_speed ?? 'balanced'
        )
      };
    } catch (caught) {
      renderSettingsError = errorMessage(caught);
    } finally {
      renderSettingsLoading = false;
    }
  }

  async function render() {
    if (!plan || !renderSettings) return;
    busy = 'render';
    error = '';
    message = '';
    try {
      renderSettings = await sessionApi.saveSettings(
        sessionId,
        'output',
        renderSettings.revision,
        { ...(renderSettings.override ?? {}), ...renderDraft }
      );
      renderOpen = false;
      const saved = await save(true);
      if (!saved) return;
      busy = 'render';
      await watchJob(
        await mediaEditApi.render(sessionId, saved.revision),
        'Rendered media is ready. Retimed subtitles were published as soon as their document revision was available.'
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

  function jumpToEditPoint(direction: -1 | 1) {
    if (!editPoints.length) return;
    const toleranceMs = 10;
    const point =
      direction < 0
        ? ([...editPoints]
            .reverse()
            .find((item) => item.timeMs < currentMs - toleranceMs) ??
          editPoints.at(-1))
        : (editPoints.find((item) => item.timeMs > currentMs + toleranceMs) ??
          editPoints[0]);
    if (point) seek(point.timeMs);
  }

  function setCutBoundary(
    rangeId: string,
    edge: CutEdge,
    value: number,
    syncVideo = false
  ) {
    if (!plan || !Number.isFinite(value)) return;
    const index = cuts.findIndex((range) => range.id === rangeId);
    if (index < 0) return;
    const range = cuts[index];
    const minimum =
      edge === 'start_ms'
        ? (cuts[index - 1]?.end_ms ?? 0)
        : range.start_ms + 20;
    const maximum =
      edge === 'start_ms'
        ? range.end_ms - 20
        : (cuts[index + 1]?.start_ms ?? plan.duration_ms);
    const nextValue = Math.max(minimum, Math.min(maximum, Math.round(value)));
    cuts = cuts.map((item, itemIndex) =>
      itemIndex === index ? { ...item, [edge]: nextValue } : item
    );
    currentMs = nextValue;
    if (syncVideo && video) video.currentTime = nextValue / 1000;
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

  function nudge(range: CutRange, edge: CutEdge, amount: number) {
    setCutBoundary(range.id, edge, range[edge] + amount, true);
  }

  onMount(load);
  onDestroy(() => {
    if (pollTimer !== undefined) window.clearTimeout(pollTimer);
    if (passivePollTimer !== undefined) window.clearTimeout(passivePollTimer);
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
          title="Choose cue-local CTC alignment, its VAD and timing controls, or an optional ASR fallback."
          class="secondary"><Settings2 size={15} /> Alignment</a
        ><button
          onclick={openProposal}
          disabled={Boolean(busy)}
          title="Create a provider-free task for a connected MCP agent, or run the proposal with a configured LLM. Nothing is rendered automatically."
          class="secondary"><Sparkles size={15} /> Process</button
        ><button
          onclick={openRender}
          disabled={Boolean(busy)}
          title="Review the encoder, resolution, quality, and speed before rendering this exact edit revision."
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

  {#if passiveRun}<section
      class="surface rounded-2xl border border-[var(--accent)]/30 p-4"
      aria-live="polite"
    >
      <div class="flex min-w-0 items-start gap-3">
        {#if passiveRun.status === 'ready'}<Bot
            class="mt-0.5 shrink-0 text-[var(--accent)]"
            size={19}
          />{:else if ['running', 'finalizing'].includes(passiveRun.status)}<LoaderCircle
            class="mt-0.5 shrink-0 animate-spin text-[var(--accent)]"
            size={19}
          />{:else if passiveRun.status === 'completed'}<Check
            class="mt-0.5 shrink-0 text-emerald-500"
            size={19}
          />{:else}<CircleAlert
            class="mt-0.5 shrink-0 text-red-500"
            size={19}
          />{/if}
        <div class="min-w-0 flex-1">
          <div class="flex flex-wrap items-center gap-2">
            <strong class="text-sm"
              >{passiveRun.status === 'ready'
                ? 'Waiting for an MCP agent'
                : passiveRun.status === 'running'
                  ? 'Agent is reviewing the recording'
                  : passiveRun.status === 'finalizing'
                    ? 'Applying the passive proposal'
                    : passiveRun.status === 'completed'
                      ? 'Passive proposal applied'
                      : 'Passive task failed'}</strong
            >
            <span class="badge">provider-free</span>
          </div>
          <p class="muted mt-1 text-xs leading-relaxed">
            {passiveRun.status === 'ready'
              ? 'This durable task is ready to be claimed through Pandrator MCP. You can leave this page open or return later.'
              : passiveRun.status === 'running'
                ? 'The transcript and exact source revision are leased to an external agent. Pandrator will reject a stale result rather than rebase it.'
                : passiveRun.status === 'finalizing'
                  ? 'The submitted cue ranges are being validated and refined against local timing evidence.'
                  : passiveRun.status === 'completed'
                    ? 'A new unreviewed edit-plan revision was created. Review every red range before rendering.'
                    : passiveRun.error_message ||
                      'The task did not create a new edit-plan revision.'}
          </p>
          <p class="muted mt-1 font-mono text-[.62rem]">
            task {passiveRun.id}
          </p>
        </div>
      </div>
    </section>{/if}

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
            completed ASR transcript. Attached captions can be aligned directly
            to the recording with cue-local CTC while their wording and speakers
            stay authoritative. Aligned words remain available in the edit plan
            and the rendered native subtitle document.
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
              'Optional: run Caption alignment for cue-local acoustic word timing'
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
              cutRanges={cuts}
              {peaks}
              {detailPeaks}
              {detailPeaksStartMs}
              {detailPeaksEndMs}
              detailLoading={Boolean(pendingDetailKey)}
              onseek={seek}
              onboundaryinput={(rangeId, edge, timeMs) =>
                setCutBoundary(rangeId, edge, timeMs)}
              onboundarycommit={(rangeId, edge, timeMs) =>
                setCutBoundary(rangeId, edge, timeMs, true)}
            />
            <div
              class="flex flex-wrap items-center justify-between gap-3 rounded-xl bg-[var(--paper)] p-3"
            >
              <div class="flex flex-wrap items-center gap-2">
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
                  class="primary self-center disabled:opacity-40"
                  ><Plus size={15} /> Add cut</button
                >
              </div>
              <div
                class="flex items-center gap-1"
                role="group"
                aria-label="Edit boundary navigation"
              >
                <button
                  onclick={() => jumpToEditPoint(-1)}
                  disabled={!editPoints.length}
                  title="Go to the previous removal edge; wraps at the beginning."
                  class="boundary-nav disabled:opacity-40"
                  ><SkipBack size={14} /> Previous edge</button
                >
                <button
                  onclick={() => jumpToEditPoint(1)}
                  disabled={!editPoints.length}
                  title="Go to the next removal edge; wraps at the end."
                  class="boundary-nav disabled:opacity-40"
                  >Next edge <SkipForward size={14} /></button
                >
              </div>
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
                      value={cut.start_ms}
                      onchange={(event) =>
                        setCutBoundary(
                          cut.id,
                          'start_ms',
                          Number(event.currentTarget.value),
                          true
                        )}
                    />
                    <small>{formatTime(cut.start_ms)}</small></label
                  ><label
                    >End
                    <input
                      type="number"
                      min="0"
                      max={plan.duration_ms}
                      step="20"
                      value={cut.end_ms}
                      onchange={(event) =>
                        setCutBoundary(
                          cut.id,
                          'end_ms',
                          Number(event.currentTarget.value),
                          true
                        )}
                    />
                    <small>{formatTime(cut.end_ms)}</small></label
                  >
                  <input
                    aria-label={`Label for cut ${index + 1}`}
                    bind:value={cut.label}
                    class="sm:col-span-2"
                  />
                  <div class="sm:col-span-2 grid gap-2">
                    <div class="boundary-control-row">
                      <span>Start</span>
                      <button
                        onclick={() => nudge(cut, 'start_ms', -100)}
                        title="Move the start edge 100 milliseconds earlier."
                        >−100 ms</button
                      >
                      <button
                        onclick={() => nudge(cut, 'start_ms', 100)}
                        title="Move the start edge 100 milliseconds later."
                        >+100 ms</button
                      >
                      <button
                        onclick={() => seek(cut.start_ms)}
                        title="Move the playhead to this start edge without playing."
                        ><LocateFixed size={12} /> Go to</button
                      >
                      <button
                        onclick={() => audition(cut.start_ms)}
                        title="Play 1.3 seconds before and after this start edge."
                        ><Play size={12} /> Preview</button
                      >
                    </div>
                    <div class="boundary-control-row">
                      <span>End</span>
                      <button
                        onclick={() => nudge(cut, 'end_ms', -100)}
                        title="Move the end edge 100 milliseconds earlier, retaining more silence before speech resumes."
                        >−100 ms</button
                      >
                      <button
                        onclick={() => nudge(cut, 'end_ms', 100)}
                        title="Move the end edge 100 milliseconds later."
                        >+100 ms</button
                      >
                      <button
                        onclick={() => seek(cut.end_ms)}
                        title="Move the playhead to this end edge without playing."
                        ><LocateFixed size={12} /> Go to</button
                      >
                      <button
                        onclick={() => audition(cut.end_ms)}
                        title="Play 1.3 seconds before and after this end edge."
                        ><Play size={12} /> Preview</button
                      >
                    </div>
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
        {#if alignmentSummary}<details
            class={alignmentSummary.reliable
              ? 'alignment-card rounded-2xl border border-emerald-400/35 bg-emerald-500/10'
              : 'alignment-card rounded-2xl border border-amber-400/40 bg-amber-500/10'}
          >
            <summary
              class="flex cursor-pointer list-none items-center gap-2 p-4 text-sm"
            >
              {#if alignmentSummary.reliable}<Check
                  size={17}
                />{:else}<CircleAlert size={17} />{/if}
              <span class="min-w-0 flex-1">
                <strong class="block">
                  {alignmentSummary.reliable
                    ? 'Caption timing applied'
                    : 'Caption timing needs attention'}
                </strong>
                <span class="muted mt-0.5 block text-xs font-normal">
                  {alignmentSummary.methodLabel} · {Math.round(
                    alignmentSummary.eligibleCoverage * 100
                  )}% of in-media words · {Math.round(
                    alignmentSummary.quality * 100
                  )}% {alignmentSummary.qualityLabel}
                </span>
              </span>
              <span class="alignment-chevron muted shrink-0"
                ><ChevronDown size={16} /></span
              >
            </summary>
            <div class="border-t border-[var(--line)] px-4 py-3">
              <p class="text-xs font-semibold leading-relaxed">
                {alignmentSummary.reliable
                  ? 'No action is required here. Acoustic word times improve cut inspection while the Zoom wording and speakers remain authoritative.'
                  : 'Rejected words retain their original caption timing and are not used for automatic boundary refinement.'}
              </p>
              {#if alignmentSummary.reliable}
                <p class="muted mt-2 text-xs leading-relaxed">
                  The render keeps a retimed word-timing artifact alongside the
                  edited subtitles. Subtitle formatting and speech-block
                  creation currently use the retimed SRT rather than that
                  word-level artifact directly.
                </p>
              {/if}
              <p class="muted mt-2 text-xs leading-relaxed">
                {alignmentSummary.methodLabel} · {Math.round(
                  alignmentSummary.coverage * 100
                )}% of all words aligned · {Math.round(
                  alignmentSummary.eligibleCoverage * 100
                )}% of in-media words · {Math.round(
                  alignmentSummary.quality * 100
                )}% {alignmentSummary.qualityLabel}{alignmentSummary.wordCount
                  ? ` · ${alignmentSummary.wordCount.toLocaleString()} timed words`
                  : ''}{alignmentSummary.cueCount
                  ? ` · ${alignmentSummary.acceptedCues.toLocaleString()}/${alignmentSummary.cueCount.toLocaleString()} cues accepted`
                  : ''}{alignmentSummary.batchCount
                  ? ` · ${alignmentSummary.batchCount.toLocaleString()} CTC batches`
                  : ''}{alignmentSummary.retryCount
                  ? ` · ${alignmentSummary.retryCount.toLocaleString()} isolated retries`
                  : ''}{alignmentSummary.outsideMediaCount
                  ? ` · ${alignmentSummary.outsideMediaCount.toLocaleString()} cues outside media`
                  : ''}{alignmentSummary.oversizedCueCount
                  ? ` · ${alignmentSummary.oversizedCueCount.toLocaleString()} oversized cues retained without CTC timing`
                  : ''}{alignmentSummary.fallbackUsed
                  ? ' · ASR fallback used'
                  : ''}.
              </p>
              {#if plan.evidence.warnings?.length}<details
                  class="mt-3 rounded-xl border border-[var(--line)] bg-[var(--paper)]"
                >
                  <summary
                    class="cursor-pointer px-3 py-2 text-xs font-semibold"
                    >{plan.evidence.warnings.length} diagnostic warning{plan
                      .evidence.warnings.length === 1
                      ? ''
                      : 's'}</summary
                  >
                  <ul class="muted list-disc space-y-1 px-7 pb-3 text-xs">
                    {#each plan.evidence.warnings as warning}<li>
                        {warning}
                      </li>{/each}
                  </ul>
                </details>{/if}
            </div>
          </details>{/if}

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
                    >{cue.timing_source.replaceAll(
                      '_',
                      ' '
                    )}{cue.timing_confidence != null
                      ? ` · ${Math.round(cue.timing_confidence * 100)}% timing quality`
                      : ''}</small
                  >
                </span>
              </button>{/each}
          </div>
        </section>

        {#if activeJob?.status === 'succeeded' && activeJob.result_json?.media_artifact_id}<section
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
            Process the transcript
          </h2>
          <p class="muted mt-2 text-sm leading-relaxed">
            Choose where the editorial reasoning runs. Pandrator refines every
            submitted cue boundary against local word timing and speech gaps;
            you still review the red ranges before rendering.
          </p>
        </div>
        <button
          onclick={() => (proposalOpen = false)}
          aria-label="Close transcript processing settings"
          class="rounded-lg p-2"><X size={19} /></button
        >
      </div>
      <div class="mt-6 grid gap-5">
        <div>
          <div class="text-sm font-semibold">Processing mode</div>
          <div
            class="mt-2 grid gap-2 sm:grid-cols-2"
            role="group"
            aria-label="Transcript processing mode"
          >
            <button
              type="button"
              class:active={proposalMode === 'passive'}
              class="proposal-mode"
              aria-pressed={proposalMode === 'passive'}
              onclick={() => (proposalMode = 'passive')}
            >
              <span class="proposal-mode-heading"
                ><Bot size={17} /> Passive agent
                <span class="badge">recommended</span></span
              >
              <span class="muted"
                >Create a durable MCP task. No configured provider or billing is
                required.</span
              >
            </button>
            <button
              type="button"
              class:active={proposalMode === 'configured'}
              class="proposal-mode"
              aria-pressed={proposalMode === 'configured'}
              onclick={() => (proposalMode = 'configured')}
            >
              <span class="proposal-mode-heading"
                ><Sparkles size={17} /> Configured LLM</span
              >
              <span class="muted"
                >Run inside Pandrator with one of your enabled provider models.</span
              >
            </button>
          </div>
        </div>
        <label class="text-sm font-semibold" for="media-edit-instructions"
          >Editing instructions<textarea
            id="media-edit-instructions"
            bind:value={instructions}
            rows="8"
            class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] p-3 font-normal leading-relaxed text-[var(--ink)]"
          ></textarea></label
        >
        {#if proposalMode === 'passive'}<div
            class="rounded-xl border border-[var(--accent)]/30 bg-[var(--accent-soft)] p-4 text-sm"
          >
            <strong
              >{passiveActive
                ? 'A passive task is already active'
                : 'What happens next'}</strong
            >
            <p class="muted mt-1 text-xs leading-relaxed">
              {passiveActive
                ? `Task ${passiveRun?.id ?? ''} is still waiting, running, or applying its result. You can monitor it above, or choose Configured LLM without creating a duplicate passive task.`
                : 'A connected agent claims one whole-recording transcript lease, submits cue ranges and reasons, and Pandrator applies the result only if this exact revision is still current. You may leave this page and return later.'}
            </p>
          </div>{:else}<div class="grid gap-3">
            <label class="text-sm font-semibold"
              >LLM model<select
                bind:value={proposalModel}
                disabled={proposalModelsLoading || !proposalModels.length}
                class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal disabled:opacity-50"
                >{#if proposalModelsLoading}<option value="default"
                    >Loading configured models…</option
                  >{:else if !proposalModels.length}<option value="default"
                    >No active models configured</option
                  >{:else}{#each proposalModels as item}<option
                      value={item.value}
                      >{item.label}{item.isDefault ? ' · default' : ''}</option
                    >{/each}{/if}</select
              ></label
            >
            {#if proposalModelsError}<p
                role="alert"
                class="text-xs text-red-500"
              >
                {proposalModelsError}
              </p>{:else if !proposalModelsLoading && !proposalModels.length}<p
                class="rounded-xl bg-amber-500/10 p-3 text-xs leading-relaxed text-amber-600"
              >
                No enabled LLM model is available. Use passive mode or configure
                a provider first.
              </p>{/if}
            <a
              href="/providers"
              class="w-fit text-xs font-semibold text-[var(--accent)]"
              >Manage LLM models and connections</a
            >
          </div>{/if}
      </div>
      <div class="mt-7 flex flex-wrap justify-end gap-3">
        <button onclick={() => (proposalOpen = false)} class="secondary"
          >Cancel</button
        ><button
          onclick={() =>
            proposalMode === 'passive' ? createPassiveProposal() : propose()}
          disabled={Boolean(busy) ||
            !instructions.trim() ||
            (proposalMode === 'passive' && passiveActive) ||
            (proposalMode === 'configured' &&
              (proposalModelsLoading || !proposalModels.length))}
          class="primary disabled:opacity-40"
          >{#if busy === 'propose' || busy === 'passive'}<LoaderCircle
              class="animate-spin"
              size={16}
            />{:else if proposalMode === 'passive'}<Bot
              size={16}
            />{:else}<Sparkles size={16} />{/if}
          {proposalMode === 'passive'
            ? passiveActive
              ? 'Agent task already active'
              : 'Create agent task'
            : 'Generate proposal'}</button
        >
      </div>
    </div>
  </div>
{/if}

{#if renderOpen}
  <div
    class="fixed inset-0 z-50 grid place-items-center bg-black/40 p-4 backdrop-blur-sm sm:p-6"
    role="presentation"
    onclick={(event) =>
      event.target === event.currentTarget && (renderOpen = false)}
  >
    <div
      use:modalFocus={{
        onclose: () => (renderOpen = false),
        initialFocus: '#media-edit-render-encoder'
      }}
      class="surface max-h-[92vh] w-full max-w-2xl overflow-y-auto rounded-[1.7rem] p-5 sm:p-7"
      role="dialog"
      aria-modal="true"
      aria-labelledby="render-title"
    >
      <div class="flex items-start justify-between gap-5">
        <div class="min-w-0">
          <div class="eyebrow">Reviewed edit</div>
          <h2 id="render-title" class="mt-1 text-2xl font-semibold">
            Render the recording
          </h2>
          <p class="muted mt-2 text-sm leading-relaxed">
            Retimed subtitles become available to correction and translation
            before the longer video encode finishes.
          </p>
        </div>
        <button
          onclick={() => (renderOpen = false)}
          aria-label="Close render settings"
          class="rounded-lg p-2"><X size={19} /></button
        >
      </div>

      {#if renderSettingsLoading}<div
          class="mt-6 flex items-center gap-3 rounded-xl border border-[var(--line)] p-4 text-sm"
        >
          <LoaderCircle class="animate-spin text-[var(--accent)]" size={17} />
          Checking FFmpeg and available encoders…
        </div>{:else if renderSettingsError}<div
          role="alert"
          class="alert error mt-6"
        >
          <CircleAlert size={18} /> <span>{renderSettingsError}</span>
        </div>{:else}<div class="mt-6 grid gap-5 sm:grid-cols-2">
          <label class="text-sm font-semibold" for="media-edit-render-encoder"
            >Video encoder<select
              id="media-edit-render-encoder"
              value={String(renderSetting('burn_video_encoder', 'libx264'))}
              onchange={(event) =>
                setRenderSetting(
                  'burn_video_encoder',
                  event.currentTarget.value
                )}
              class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
              >{#each renderEncoderOptions as item}<option value={item.id}
                  >{item.label}{item.hardware ? ' · verified' : ''}</option
                >{/each}</select
            ></label
          >
          <label class="text-sm font-semibold"
            >Encoding speed<select
              value={String(renderSetting('burn_video_speed', 'balanced'))}
              onchange={(event) =>
                setRenderSetting('burn_video_speed', event.currentTarget.value)}
              class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
              ><option value="fast">Fast</option><option value="balanced"
                >Balanced</option
              ><option value="quality">Quality</option></select
            ></label
          >
          <label class="text-sm font-semibold"
            >Resolution<select
              value={String(renderSetting('burn_video_resolution', 'source'))}
              onchange={(event) =>
                setRenderSetting(
                  'burn_video_resolution',
                  event.currentTarget.value
                )}
              class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
              ><option value="source">Source resolution</option><option
                value="2160p">2160p</option
              ><option value="1440p">1440p</option><option value="1080p"
                >1080p</option
              ><option value="720p">720p</option><option value="480p"
                >480p</option
              ></select
            ></label
          >
          <label class="text-sm font-semibold"
            >Quality <span class="muted font-normal">(lower is better)</span>
            <div class="mt-2 flex items-center gap-3">
              <input
                type="range"
                min="0"
                max="51"
                value={Number(renderSetting('burn_video_quality', 18))}
                oninput={(event) =>
                  setRenderSetting(
                    'burn_video_quality',
                    Number(event.currentTarget.value)
                  )}
                class="min-w-0 flex-1"
              />
              <span class="w-8 text-right tabular-nums"
                >{Number(renderSetting('burn_video_quality', 18))}</span
              >
            </div></label
          >
          {#if !verifiedHardwareEncoderAvailable}<div
              class="rounded-xl bg-amber-500/10 p-4 text-sm leading-relaxed text-amber-700 sm:col-span-2"
            >
              No hardware encoder passed a real FFmpeg encode probe. Software
              encoding is available; Fast trades a larger file for less waiting.
            </div>{/if}
        </div>{/if}

      <div class="mt-7 flex flex-wrap justify-end gap-3">
        <button onclick={() => (renderOpen = false)} class="secondary"
          >Cancel</button
        ><button
          onclick={render}
          disabled={Boolean(busy) ||
            renderSettingsLoading ||
            Boolean(renderSettingsError) ||
            !renderSettings}
          class="primary disabled:opacity-40"
          >{#if busy === 'render'}<LoaderCircle
              class="animate-spin"
              size={16}
            />{:else}<Scissors size={16} />{/if}
          Start render</button
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
  .proposal-mode {
    display: grid;
    gap: 0.5rem;
    min-width: 0;
    border: 1px solid var(--line);
    border-radius: 0.85rem;
    background: var(--paper);
    padding: 0.85rem;
    text-align: left;
    font-size: 0.72rem;
    line-height: 1.45;
  }
  .proposal-mode:hover,
  .proposal-mode.active {
    border-color: var(--accent);
    background: var(--accent-soft);
  }
  .proposal-mode.active {
    box-shadow: 0 0 0 1px var(--accent);
  }
  .proposal-mode-heading {
    display: flex;
    min-width: 0;
    align-items: center;
    gap: 0.45rem;
    font-size: 0.8rem;
    font-weight: 750;
  }
  .proposal-mode-heading .badge {
    margin-left: auto;
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
  .boundary-nav {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    border: 1px solid var(--line);
    border-radius: 0.65rem;
    background: var(--paper-strong);
    padding: 0.55rem 0.65rem;
    font-size: 0.68rem;
    font-weight: 700;
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
  .boundary-control-row {
    display: flex;
    min-width: 0;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.25rem;
  }
  .boundary-control-row > span {
    width: 2.5rem;
    color: var(--muted);
    font-size: 0.62rem;
    font-weight: 750;
    text-transform: uppercase;
  }
  .alignment-card > summary::-webkit-details-marker {
    display: none;
  }
  .alignment-chevron {
    transition: transform 160ms ease;
  }
  .alignment-card[open] .alignment-chevron {
    transform: rotate(180deg);
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
