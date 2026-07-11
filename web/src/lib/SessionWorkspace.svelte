<script lang="ts">
  import {
    ArrowLeft,
    Check,
    ChevronRight,
    CircleAlert,
    Clock3,
    Crop,
    FileUp,
    LoaderCircle,
    Play,
    Settings2,
    Sparkles,
    X
  } from '@lucide/svelte';
  import { api, type JobRecord, type SessionRecord } from './api';
  import PdfEditor from './PdfEditor.svelte';
  import SubtitleReview from './SubtitleReview.svelte';
  import GuidedTour from './GuidedTour.svelte';
  import { onMount } from 'svelte';

  type Stage = {
    number: number;
    key: string;
    title: string;
    explanation: string;
    status: 'unavailable' | 'ready' | 'running' | 'completed' | 'stale' | 'failed';
    executable: boolean;
    included: boolean;
    required?: boolean;
    artifact?: { id: string; role: string; path: string } | null;
    job_id?: string | null;
    progress?: number | null;
    detail?: string | null;
  };

  type Snapshot = {
    session_id: string;
    workflow_kind: string;
    workflow_preset: string;
    revision: number;
    stages: Stage[];
    sources: { id: string; filename: string; kind: string; role: string }[];
  };

  let { session, onback, onupdated }: { session: SessionRecord; onback: () => void; onupdated: (session: SessionRecord) => void } = $props();
  let snapshot = $state<Snapshot | null>(null);
  let loading = $state(true);
  let error = $state('');
  let uploading = $state(false);
  let settingsStage = $state<Stage | null>(null);
  let stageSettings = $state<Record<string, Record<string, unknown>>>({});
  let targetLanguage = $state('en');
  let originalLanguage = $state('auto');
  let model = $state('default');
  let backend = $state('whisperx');
  let instructions = $state('');
  let agentic = $state(false);
  let maxIterations = $state(53);
  let ttsService = $state('XTTS');
  let voiceName = $state('');
  let subtitleMode = $state('soft');
  let audioMode = $state('preserve');
  let pdfSource = $state<{ id: string; filename: string } | null>(null);
  let reviewOpen = $state(false);
  let refreshTimer: number | undefined;
  let workflowTour = $state(false);
  const workflowTourSteps = [
    {section:'Workflow',title:'Stages are independent',body:'Run any ready card on its own. Its latest artifact, settings, and status stay attached to that stage.'},
    {section:'Workflow',title:'Inclusion composes the outcome',body:'Included stages are checked for missing or stale outputs when Generate audio continues the workflow. Completed matching artifacts are reused.'},
    {section:'Review',title:'Preview before synthesis',body:'Subtitle comparison aligns transcription, correction, and translation, including split and merged lineage. Saving creates a reviewed revision.'},
    {section:'Export',title:'Export does not require dubbing',body:'Subtitle-only exports preserve source audio. When dubbing exists, choose source, mixed, or dubbing-only audio and soft or burned subtitles.'}
  ];

  async function load() {
    loading = true;
    try {
      snapshot = await api<Snapshot>(`/sessions/${session.id}/workflow`);
    } catch (caught) {
      error = caught instanceof Error ? caught.message : String(caught);
    } finally {
      loading = false;
    }
  }

  async function toggleIncluded(stage: Stage) {
    const current = new Set(session.included_stages_json);
    if (current.has(stage.key)) current.delete(stage.key); else current.add(stage.key);
    try {
      const updated = await api<SessionRecord>(`/sessions/${session.id}`, {
        method: 'PATCH',
        headers: { 'If-Match': `"${session.revision}"` },
        body: JSON.stringify({ included_stages: [...current] })
      });
      session = updated;
      onupdated(updated);
      await load();
    } catch (caught) {
      error = caught instanceof Error ? caught.message : String(caught);
    }
  }

  async function run(stage: Stage) {
    if (stage.key === 'preview') {
      reviewOpen = true;
      return;
    }
    error = '';
    try {
      await api<JobRecord>(`/sessions/${session.id}/stages/${stage.key}/run`, {
        method: 'POST',
        body: JSON.stringify(stage.key === 'generate_audio' ? { ...(stageSettings[stage.key] ?? {}), stage_settings: stageSettings } : (stageSettings[stage.key] ?? {}))
      });
      await load();
    } catch (caught) {
      error = caught instanceof Error ? caught.message : String(caught);
    }
  }

  async function upload(event: Event) {
    const input = event.currentTarget as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return;
    uploading = true;
    error = '';
    const body = new FormData();
    body.set('session_id', session.id);
    body.set('file', file);
    try {
      await api('/uploads', { method: 'POST', body });
      await load();
    } catch (caught) {
      error = caught instanceof Error ? caught.message : String(caught);
    } finally {
      uploading = false;
      input.value = '';
    }
  }

  function openSettings(stage: Stage) {
    settingsStage = stage;
    const saved = stageSettings[stage.key] ?? {};
    targetLanguage = String(saved.target_language ?? 'en');
    originalLanguage = String(saved.original_language ?? 'auto');
    model = String(saved.model_name ?? saved[`${stage.key}_model`] ?? 'default');
    backend = String(saved.backend ?? saved.translation_backend ?? 'whisperx');
    instructions = String(saved.instructions ?? '');
    agentic = Boolean(saved.agentic ?? false);
    maxIterations = Number(saved.max_iterations ?? 53);
    ttsService = String(saved.tts_service ?? saved.service ?? 'XTTS');
    voiceName = String(saved.voice ?? saved.voice_name ?? '');
    subtitleMode = String(saved.subtitle_mode ?? 'soft');
    audioMode = String(saved.audio_mode ?? 'preserve');
  }

  async function cancel(stage: Stage) {
    if (!stage.job_id) return;
    try { await api(`/jobs/${stage.job_id}/cancel`, { method: 'POST' }); await load(); }
    catch (caught) { error = caught instanceof Error ? caught.message : String(caught); }
  }

  function saveSettings() {
    if (!settingsStage) return;
    const key = settingsStage.key;
    const common = { model_name: model === 'default' ? '' : model, [`${key}_model`]: model };
    if (key === 'transcribe') stageSettings[key] = { backend, original_language: originalLanguage };
    else if (key === 'correct') stageSettings[key] = { ...common, instructions };
    else if (key === 'translate') stageSettings[key] = { ...common, translation_backend: backend, target_language: targetLanguage, instructions };
    else if (key === 'clean_source') stageSettings[key] = { ...common, agentic, max_iterations: maxIterations };
    else if (key === 'prepare_text' || key === 'generate_audio') stageSettings[key] = { tts_service: ttsService, service: ttsService, voice: voiceName, language: targetLanguage };
    else if (key === 'export') stageSettings[key] = { subtitle_mode: subtitleMode, audio_mode: audioMode };
    else stageSettings[key] = common;
    settingsStage = null;
  }

  const statusIcon = (status: Stage['status']) => {
    if (status === 'completed') return Check;
    if (status === 'running') return LoaderCircle;
    if (status === 'stale' || status === 'failed') return CircleAlert;
    return Clock3;
  };

  onMount(load);
  $effect(() => {
    if (refreshTimer) window.clearTimeout(refreshTimer);
    if (snapshot?.stages.some((stage) => stage.status === 'running')) refreshTimer = window.setTimeout(load, 1000);
  });
</script>

<div class="mx-auto max-w-6xl">
  <button onclick={onback} class="muted mb-7 flex items-center gap-2 text-sm font-semibold"><ArrowLeft size={17}/> All sessions</button>
  <header class="mb-8 flex flex-wrap items-end justify-between gap-6">
    <div><div class="eyebrow mb-2 capitalize">{session.workflow_kind} workflow</div><h1 class="text-3xl font-semibold tracking-[-.035em] sm:text-4xl">{session.name}</h1><p class="muted mt-3 max-w-2xl">Run each stage independently, or include it when continuing toward the final result.</p></div>
    <div class="flex flex-wrap gap-2"><button onclick={() => workflowTour=true} class="lift flex items-center gap-2 rounded-xl border border-[var(--line)] bg-[var(--paper-strong)] px-4 py-3 text-sm font-semibold"><Sparkles size={17}/> Tour</button>{#if snapshot?.sources.find((item) => item.filename.toLowerCase().endsWith('.pdf'))}{@const availablePdf = snapshot.sources.find((item) => item.filename.toLowerCase().endsWith('.pdf'))!}<button onclick={() => pdfSource=availablePdf} class="lift flex items-center gap-3 rounded-xl border border-[var(--line)] bg-[var(--paper-strong)] px-4 py-3 text-sm font-semibold"><Crop size={18}/> Edit PDF</button>{/if}<label class="lift flex cursor-pointer items-center gap-3 rounded-xl border border-[var(--line)] bg-[var(--paper-strong)] px-4 py-3 text-sm font-semibold">
      {#if uploading}<LoaderCircle class="animate-spin" size={18}/>{:else}<FileUp size={18}/>{/if}
      {uploading ? 'Importing…' : 'Add source'}
      <input type="file" class="sr-only" onchange={upload} disabled={uploading}/>
    </label></div>
  </header>

  {#if error}<div class="mb-5 flex items-start gap-3 rounded-xl border border-red-400/40 bg-red-500/10 px-4 py-3 text-sm"><CircleAlert class="mt-0.5 shrink-0" size={17}/><span>{error}</span></div>{/if}

  {#if loading}
    <div class="surface grid min-h-64 place-items-center rounded-3xl"><LoaderCircle class="animate-spin text-[var(--accent)]" size={28}/></div>
  {:else if snapshot}
    <div class="space-y-4">
      {#each snapshot.stages as stage}
        {@const StatusIcon = statusIcon(stage.status)}
        <article class="surface rounded-[1.4rem] p-5 sm:p-6">
          <div class="flex flex-col gap-5 lg:flex-row lg:items-center">
            <div class="flex min-w-0 flex-1 items-start gap-4">
              <div class="grid size-11 shrink-0 place-items-center rounded-2xl bg-[var(--accent-soft)] text-sm font-bold text-[var(--accent)]">{stage.number}</div>
              <div class="min-w-0"><div class="flex flex-wrap items-center gap-2"><h2 class="text-lg font-semibold">{stage.title}</h2><span class:running={stage.status === 'running'} class:done={stage.status === 'completed'} class:warning={stage.status === 'stale' || stage.status === 'failed'} class="status-chip inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[.68rem] font-bold uppercase tracking-wider"><StatusIcon class={stage.status === 'running' ? 'animate-spin' : ''} size={12}/>{stage.status}</span></div><p class="muted mt-1.5 max-w-2xl text-sm leading-relaxed">{stage.explanation}</p>{#if stage.status==='running' && stage.progress!=null}<div class="mt-3 h-1.5 max-w-md overflow-hidden rounded-full bg-[var(--line)]"><div class="h-full bg-[var(--accent)]" style={`width:${Math.max(2,stage.progress*100)}%`}></div></div>{/if}{#if stage.detail}<p class="mt-2 text-xs text-red-500">{stage.detail}</p>{/if}{#if stage.artifact}<button class="mt-2 flex items-center gap-1 text-xs font-semibold text-[var(--accent)]">Latest: {stage.artifact.role}<ChevronRight size={13}/></button>{/if}</div>
            </div>
            <div class="flex flex-wrap items-center gap-2 lg:justify-end">
              {#if stage.executable}
                <label class="muted flex items-center gap-2 rounded-lg px-2 py-2 text-xs font-semibold" class:cursor-pointer={!stage.required} title={stage.required?'Required by the selected outcome':''}><input type="checkbox" checked={stage.included} disabled={stage.required} onchange={() => toggleIncluded(stage)} class="size-4 accent-[var(--accent)] disabled:opacity-60"/> {stage.required?'Required for outcome':'Include when continuing'}</label>
                <button onclick={() => openSettings(stage)} class="flex items-center gap-2 rounded-xl border border-[var(--line)] px-3.5 py-2.5 text-sm font-semibold"><Settings2 size={16}/> Settings</button>
                {#if stage.status==='running'}<button onclick={() => cancel(stage)} class="rounded-xl border border-red-400/50 px-4 py-2.5 text-sm font-semibold text-red-500">Cancel</button>{:else}<button onclick={() => run(stage)} disabled={stage.status === 'unavailable'} class="flex items-center gap-2 rounded-xl bg-[var(--accent)] px-4 py-2.5 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:opacity-35"><Play size={16}/> Run now</button>{/if}
              {:else}
                <button onclick={() => run(stage)} disabled={stage.status === 'unavailable'} class="flex items-center gap-2 rounded-xl bg-[var(--accent)] px-4 py-2.5 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:opacity-35"><Sparkles size={16}/> Open comparison</button>
              {/if}
            </div>
          </div>
        </article>
      {/each}
    </div>
  {/if}
</div>

{#if settingsStage}
  <div class="fixed inset-0 z-50 grid place-items-center bg-black/35 p-5 backdrop-blur-sm" role="presentation" onclick={(event) => event.target === event.currentTarget && (settingsStage=null)}>
    <div class="surface w-full max-w-lg rounded-[1.7rem] p-7" role="dialog" aria-modal="true" aria-labelledby="settings-title">
      <div class="flex justify-between gap-5"><div><div class="eyebrow">Stage settings</div><h2 id="settings-title" class="mt-1 text-2xl font-semibold">{settingsStage.title}</h2></div><button onclick={() => settingsStage=null} class="rounded-lg p-2"><X size={19}/></button></div>
      <div class="mt-6 grid gap-5">
        {#if ['correct','translate','clean_source'].includes(settingsStage.key)}<label class="text-sm font-semibold">Model<input bind:value={model} placeholder="Use configured default" class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"/></label>{/if}
        {#if settingsStage.key === 'transcribe'}<label class="text-sm font-semibold">STT backend<select bind:value={backend} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"><option value="whisperx">WhisperX</option><option value="parakeet_onnx">ONNX Parakeet</option></select></label><label class="text-sm font-semibold">Source language<input bind:value={originalLanguage} placeholder="auto" class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"/></label>{/if}
        {#if settingsStage.key === 'correct'}<label class="text-sm font-semibold">Correction guidance<textarea bind:value={instructions} rows="4" class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"></textarea></label>{/if}
        {#if settingsStage.key === 'translate'}<label class="text-sm font-semibold">Translation backend<select bind:value={backend} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"><option value="llm">LLM</option><option value="deepl">DeepL</option></select></label><label class="text-sm font-semibold">Target language<input bind:value={targetLanguage} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"/></label><label class="text-sm font-semibold">Translation guidance<textarea bind:value={instructions} rows="3" class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"></textarea></label>{/if}
        {#if settingsStage.key === 'clean_source'}<label class="flex items-start gap-3 rounded-xl border border-[var(--line)] p-4"><input type="checkbox" bind:checked={agentic} class="mt-1 size-4 accent-[var(--accent)]"/><span><span class="block text-sm font-semibold">Agentic review loop</span><span class="muted mt-1 block text-xs">Runs focused metadata, navigation, boilerplate, repeated-element, and chapter passes. Provider costs may apply.</span></span></label>{#if agentic}<label class="text-sm font-semibold">Maximum LLM turns<input type="number" min="5" max="500" bind:value={maxIterations} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"/></label>{/if}{/if}
        {#if ['prepare_text','generate_audio'].includes(settingsStage.key)}<label class="text-sm font-semibold">TTS service<select bind:value={ttsService} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"><option>XTTS</option><option>Kokoro</option><option>Chatterbox</option><option>VoxCPM</option><option>Fish Speech 2</option><option>Silero</option></select></label><label class="text-sm font-semibold">Voice / model<input bind:value={voiceName} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"/></label><label class="text-sm font-semibold">Language<input bind:value={targetLanguage} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"/></label>{/if}
        {#if settingsStage.key === 'export'}<label class="text-sm font-semibold">Audio<select bind:value={audioMode} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"><option value="preserve">Preserve source audio</option><option value="mixed">Mix source and dubbing</option><option value="dubbing_only">Dubbing only</option></select></label><label class="text-sm font-semibold">Subtitles<select bind:value={subtitleMode} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"><option value="none">None</option><option value="soft">Injected soft tracks</option><option value="burn">Burned bilingual overlay</option></select></label>{/if}
      </div>
      <div class="mt-7 flex justify-end gap-3"><button onclick={() => settingsStage=null} class="rounded-xl border border-[var(--line)] px-4 py-2.5 text-sm font-semibold">Cancel</button><button onclick={saveSettings} class="rounded-xl bg-[var(--accent)] px-4 py-2.5 text-sm font-semibold text-white">Save settings</button></div>
    </div>
  </div>
{/if}

{#if pdfSource}<PdfEditor sessionId={session.id} source={pdfSource} onclose={() => pdfSource=null}/>{/if}
{#if reviewOpen}<SubtitleReview sessionId={session.id} sourceArtifactId={snapshot?.sources[0]?.id} onclose={() => reviewOpen=false} onsaved={load}/>{/if}
<GuidedTour tourId="workflow" steps={workflowTourSteps} bind:open={workflowTour}/>

<style>
  .status-chip { color: var(--muted); background: color-mix(in srgb, var(--muted) 10%, transparent); }
  .status-chip.done { color: var(--success); background: color-mix(in srgb, var(--success) 12%, transparent); }
  .status-chip.running { color: var(--accent); background: var(--accent-soft); }
  .status-chip.warning { color: var(--warning); background: color-mix(in srgb, var(--warning) 12%, transparent); }
</style>
