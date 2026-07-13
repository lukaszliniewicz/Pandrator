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
  import ArtifactPreview from './ArtifactPreview.svelte';
  import SettingsModal from './SettingsModal.svelte';
  import TtsServicesModal from './TtsServicesModal.svelte';
  import { artifactRoleLabel, type PreviewableArtifact } from './artifact-display';
  import { LANGUAGE_OPTIONS } from './settings-fields';
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
    artifact?: { id: string; role: string; raw_role?: string; path: string } | null;
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
  let outcome = $state<any>(null);
  let capabilities = $state<Record<string, any>>({});
  let ttsCatalogue = $state<any>({services:[]});
  let libraryVoices = $state<any[]>([]);
  let loading = $state(true);
  let error = $state('');
  let uploading = $state(false);
  let settingsStage = $state<Stage | null>(null);
  let fullSettingsSection = $state('');
  let ttsServicesOpen = $state(false);
  let preview = $state<PreviewableArtifact | null>(null);
  const sectionDisplay = (section: string) => ({ stt: 'STT', tts: 'TTS', rvc: 'RVC' } as Record<string, string>)[section] ?? section.replaceAll('_', ' ');
  let stageSettings = $state<Record<string, Record<string, unknown>>>({});
  let targetLanguage = $state('en');
  let originalLanguage = $state('auto');
  let model = $state('default');
  let backend = $state('llm');
  let sttEngine = $state('whisper');
  let sttQuantization = $state('f16');
  let sttComputeBackend = $state('auto');
  let sttDevice = $state(0);
  let sttThreads = $state(0);
  let sttChunkSeconds = $state(0);
  let sttChunkOverlap = $state(3);
  let sttHotwords = $state('');
  let sttLidBackend = $state('whisper');
  let sttBeamSize = $state(1);
  let parakeetDecoder = $state('tdt');
  let vadEnabled = $state(true);
  let vadModel = $state('silero');
  let vadThreshold = $state(0.5);
  let vadMinSpeech = $state(250);
  let vadMinSilence = $state(100);
  let vadMaxSpeech = $state(300);
  let vadSpeechPad = $state(30);
  let subtitleChars = $state(48);
  let subtitleLines = $state(2);
  let subtitleMinDuration = $state(833);
  let subtitleMaxDuration = $state(7000);
  let subtitleCps = $state(20);
  let subtitleMinGap = $state(80);
  let subtitlePhraseGap = $state(600);
  let instructions = $state('');
  let optimizationPrompt = $state('');
  let optimizationConcurrent = $state(1);
  let optimizationMultiStage = $state(false);
  let optimizationFirstPrompt = $state('');
  let optimizationSecondPrompt = $state('');
  let optimizationThirdPrompt = $state('');
  let agentic = $state(false);
  let maxIterations = $state(53);
  let ttsService = $state('XTTS');
  let ttsModel = $state('');
  let voiceName = $state('');
  let speechBlockMinChars = $state(10);
  let speechBlockMaxChars = $state(160);
  let speechBlockMergeThreshold = $state(250);
  let subtitleMode = $state('soft');
  let subtitleSelection = $state('dual');
  let audioMode = $state('preserve');
  let pdfSource = $state<{ id: string; filename: string } | null>(null);
  let reviewOpen = $state(false);
  let refreshTimer: number | undefined;
  let workflowTour = $state(false);
  const workflowTourSteps = [
    {section:'Workflow',title:'Stages are independent',body:'Run any ready card on its own. Its latest artifact, settings, and status stay attached to that stage.'},
    {section:'Workflow',title:'The outcome composes the pipeline',body:'Customize Workflow chooses meaningful transformations and deliverables. Run Now remains available on every ready transformation.'},
    {section:'Review',title:'Preview before synthesis',body:'Subtitle comparison aligns transcription, correction, and translation, including split and merged lineage. Saving creates a reviewed revision.'},
    {section:'Export',title:'Export does not require dubbing',body:'Subtitle-only exports preserve source audio. When dubbing exists, choose source, mixed, or dubbing-only audio and soft or burned subtitles.'}
  ];

  async function load() {
    loading = true;
    try {
      [snapshot, outcome] = await Promise.all([api<Snapshot>(`/sessions/${session.id}/workflow`), api(`/sessions/${session.id}/outcome-plan`)]);
      for (const stage of snapshot?.stages ?? []) {
        if (stage.artifact) {
          stage.artifact.raw_role = stage.artifact.role;
          stage.artifact.role = artifactRoleLabel(stage.artifact.role);
        }
      }
    } catch (caught) {
      error = caught instanceof Error ? caught.message : String(caught);
    } finally {
      loading = false;
    }
  }

  async function loadCapabilities() {
    try { capabilities = await api<Record<string, any>>('/capabilities'); }
    catch { capabilities = {}; }
  }

  const supportsSttCompute = (name: string) => name === 'auto' || (capabilities?.stt?.compute_backends ?? []).includes(name);

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

  const stageSection = (key: string) => ({transcribe:'stt',correct:'correction',translate:'translation',optimize_tts:'text',clean_source:'source_cleaning',prepare_text:'tts',generate_audio:'tts',export:'output'}[key] ?? 'text');

  async function openSettings(stage: Stage) {
    settingsStage = stage;
    let saved = stageSettings[stage.key] ?? {};
    try {
      const stored = await api<any>(`/sessions/${session.id}/settings/${stageSection(stage.key)}`);
      saved = {...stored.effective, ...saved};
      stageSettings[stage.key] = saved;
    } catch { /* use stage-local values */ }
    targetLanguage = String(saved.target_language ?? 'en');
    originalLanguage = String(saved.original_language ?? 'auto');
    model = String(saved.model_name ?? saved[`${stage.key}_model`] ?? 'default');
    backend = String(saved.backend ?? saved.translation_backend ?? 'llm');
    sttEngine = String(saved.stt_engine ?? saved.stt_backend ?? 'whisper').includes('parakeet') ? 'parakeet' : 'whisper';
    sttQuantization = String(saved.stt_model_quantization ?? 'f16');
    sttComputeBackend = String(saved.stt_compute_backend ?? 'auto');
    sttDevice = Number(saved.stt_compute_device ?? 0);
    sttThreads = Number(saved.stt_threads ?? 0);
    sttChunkSeconds = Number(saved.stt_chunk_seconds ?? 0);
    sttChunkOverlap = Number(saved.stt_chunk_overlap_seconds ?? 3);
    sttHotwords = String(saved.stt_hotwords ?? '');
    sttLidBackend = String(saved.stt_lid_backend ?? 'whisper');
    sttBeamSize = Number(saved.stt_beam_size ?? 1);
    parakeetDecoder = String(saved.parakeet_decoder ?? 'tdt');
    vadEnabled = Boolean(saved.crispasr_vad_enabled ?? true);
    vadModel = String(saved.crispasr_vad_model ?? 'silero');
    vadThreshold = Number(saved.crispasr_vad_threshold ?? 0.5);
    vadMinSpeech = Number(saved.crispasr_vad_min_speech_ms ?? 250);
    vadMinSilence = Number(saved.crispasr_vad_min_silence_ms ?? 100);
    vadMaxSpeech = Number(saved.crispasr_vad_max_speech_seconds ?? 300);
    vadSpeechPad = Number(saved.crispasr_vad_speech_pad_ms ?? 30);
    subtitleChars = Number(saved.subtitle_max_chars_per_line ?? 48);
    subtitleLines = Number(saved.subtitle_max_lines ?? 2);
    subtitleMinDuration = Number(saved.subtitle_min_duration_ms ?? 833);
    subtitleMaxDuration = Number(saved.subtitle_max_duration_ms ?? 7000);
    subtitleCps = Number(saved.subtitle_max_cps ?? 20);
    subtitleMinGap = Number(saved.subtitle_min_gap_ms ?? 80);
    subtitlePhraseGap = Number(saved.subtitle_phrase_gap_ms ?? 600);
    instructions = String(saved.instructions ?? '');
    optimizationPrompt = String(saved.combined_prompt ?? '');
    optimizationConcurrent = Number(saved.llm_concurrent_calls ?? 1);
    optimizationMultiStage = Boolean(saved.llm_multi_stage ?? false);
    optimizationFirstPrompt = String(saved.first_prompt ?? '');
    optimizationSecondPrompt = String(saved.second_prompt ?? '');
    optimizationThirdPrompt = String(saved.third_prompt ?? '');
    agentic = Boolean(saved.agentic ?? false);
    maxIterations = Number(saved.max_iterations ?? 53);
    ttsService = String(saved.tts_service ?? saved.service ?? 'XTTS');
    ttsModel = String(saved.model ?? saved.xtts_model ?? '');
    voiceName = String(saved.voice ?? saved.voice_name ?? '');
    speechBlockMinChars = Number(saved.speech_block_min_chars ?? 10);
    speechBlockMaxChars = Number(saved.speech_block_max_chars ?? 160);
    speechBlockMergeThreshold = Number(saved.speech_block_merge_threshold ?? 250);
    subtitleMode = String(saved.subtitle_mode ?? 'soft');
    subtitleSelection = String(saved.subtitle_selection ?? 'dual');
    audioMode = String(saved.audio_mode ?? 'preserve');
  }

  async function cancel(stage: Stage) {
    if (!stage.job_id) return;
    try { await api(`/jobs/${stage.job_id}/cancel`, { method: 'POST' }); await load(); }
    catch (caught) { error = caught instanceof Error ? caught.message : String(caught); }
  }

  async function persistSection(section:string,value:Record<string,unknown>) {
    const stored = await api<any>(`/sessions/${session.id}/settings/${section}`);
    return api(`/sessions/${session.id}/settings/${section}`,{method:'PUT',headers:{'If-Match':`"${stored.revision}"`},body:JSON.stringify({value:{...stored.override,...value}})});
  }

  async function loadSpeechCatalogues() {
    try {
      const [services, voices] = await Promise.all([api<any>('/services/tts'), api<any>('/voices')]);
      ttsCatalogue = services;
      libraryVoices = voices.items ?? [];
    } catch {
      ttsCatalogue = {services:[]};
      libraryVoices = [];
    }
  }

  const selectedTtsService = $derived((ttsCatalogue.services??[]).find((item:any)=>[item.id,item.name].map((value)=>String(value??'').toLowerCase()).includes(ttsService.toLowerCase())));
  const ttsModels = $derived(selectedTtsService?.models??[]);
  const selectedTtsDefaultVoice = $derived(selectedTtsService?.default_voices?.[ttsModel] ?? selectedTtsService?.default_voice ?? '');
  const ttsVoices = $derived(Array.from(new Set([...(selectedTtsService?.voice_catalogues?.[ttsModel] ?? selectedTtsService?.voices ?? []),...libraryVoices.map((voice:any)=>voice.name)])));

  function previewArtifact(stage: Stage) {
    if (!stage.artifact) return;
    const role = stage.artifact.raw_role ?? stage.artifact.role;
    if (['transcription','correction','translation','tts_optimized'].includes(role)) {
      reviewOpen = true;
      return;
    }
    preview = { id: stage.artifact.id, role, relative_path: stage.artifact.path };
  }

  async function saveSettings() {
    if (!settingsStage) return;
    const key = settingsStage.key;
    const common = { model_name: model === 'default' ? '' : model, [`${key}_model`]: model };
    if (key === 'transcribe') stageSettings[key] = {
      stt_engine: sttEngine, stt_backend: sttEngine, stt_model_quantization: sttQuantization,
      stt_compute_backend: sttComputeBackend,
      stt_compute_device: sttDevice, stt_language: originalLanguage, original_language: originalLanguage,
      stt_threads: sttThreads, stt_chunk_seconds: sttChunkSeconds,
      stt_chunk_overlap_seconds: sttChunkOverlap, stt_hotwords: sttHotwords,
      stt_lid_backend: sttLidBackend, stt_beam_size: sttBeamSize,
      parakeet_decoder: parakeetDecoder,
      crispasr_vad_enabled: vadEnabled, crispasr_vad_model: vadModel,
      crispasr_vad_threshold: vadThreshold,
      crispasr_vad_min_speech_ms: vadMinSpeech, crispasr_vad_min_silence_ms: vadMinSilence,
      crispasr_vad_max_speech_seconds: vadMaxSpeech, crispasr_vad_speech_pad_ms: vadSpeechPad,
      subtitle_max_chars_per_line: subtitleChars, subtitle_max_lines: subtitleLines,
      subtitle_min_duration_ms: subtitleMinDuration, subtitle_max_duration_ms: subtitleMaxDuration,
      subtitle_max_cps: subtitleCps, subtitle_min_gap_ms: subtitleMinGap,
      subtitle_phrase_gap_ms: subtitlePhraseGap
    };
    else if (key === 'correct') stageSettings[key] = { ...common, instructions };
    else if (key === 'translate') stageSettings[key] = { ...common, translation_backend: backend, target_language: targetLanguage, instructions };
    else if (key === 'optimize_tts') stageSettings[key] = { ...common, combined_prompt:optimizationPrompt, llm_concurrent_calls:optimizationConcurrent, llm_multi_stage:optimizationMultiStage, first_prompt:optimizationFirstPrompt, second_prompt:optimizationSecondPrompt, third_prompt:optimizationThirdPrompt };
    else if (key === 'clean_source') stageSettings[key] = { ...common, agentic, max_iterations: maxIterations };
    else if (key === 'prepare_text') stageSettings[key] = { tts_service: ttsService, service: ttsService, model: ttsModel, xtts_model: ttsModel, voice: voiceName, language: targetLanguage };
    else if (key === 'generate_audio') stageSettings[key] = {
      tts_service: ttsService, service: ttsService, model: ttsModel, xtts_model: ttsModel, voice: voiceName,
      language: targetLanguage, target_language: targetLanguage,
      speech_block_min_chars: speechBlockMinChars,
      speech_block_max_chars: speechBlockMaxChars,
      speech_block_merge_threshold: speechBlockMergeThreshold
    };
    else if (key === 'export') stageSettings[key] = {
      subtitle_mode: subtitleMode, subtitle_selection: subtitleSelection, audio_mode: audioMode,
      subtitle_max_chars_per_line: subtitleChars, subtitle_max_lines: subtitleLines,
      subtitle_min_duration_ms: subtitleMinDuration, subtitle_max_duration_ms: subtitleMaxDuration,
      subtitle_max_cps: subtitleCps, subtitle_min_gap_ms: subtitleMinGap,
      subtitle_phrase_gap_ms: subtitlePhraseGap
    };
    else stageSettings[key] = common;
    try {
      if (key === 'transcribe') {
        await persistSection('stt', stageSettings[key]);
        await persistSection('subtitles',{max_chars_per_line:subtitleChars,max_lines:subtitleLines,min_duration_ms:subtitleMinDuration,max_duration_ms:subtitleMaxDuration,max_cps:subtitleCps,min_gap_ms:subtitleMinGap,phrase_gap_ms:subtitlePhraseGap});
      } else if (key === 'correct') await persistSection('correction',{enabled:true,model_name:model==='default'?'':model,instructions});
      else if (key === 'translate') await persistSection('translation',{enabled:true,backend,target_language:targetLanguage,model_name:model==='default'?'':model,instructions});
      else if (key === 'optimize_tts') await persistSection('text',{llm_tts_optimization:true,llm_processing_enabled:true,llm_concurrent_calls:optimizationConcurrent,llm_multi_stage:optimizationMultiStage,combined_prompt:optimizationPrompt,first_prompt:optimizationFirstPrompt,second_prompt:optimizationSecondPrompt,third_prompt:optimizationThirdPrompt});
      else await persistSection(stageSection(key),stageSettings[key]);
      settingsStage = null;
    } catch (caught) { error=caught instanceof Error?caught.message:String(caught); }
  }

  const statusIcon = (status: Stage['status']) => {
    if (status === 'completed') return Check;
    if (status === 'running') return LoaderCircle;
    if (status === 'stale' || status === 'failed') return CircleAlert;
    return Clock3;
  };

  onMount(async () => { await Promise.all([load(), loadCapabilities(), loadSpeechCatalogues()]); });
  $effect(() => {
    if (refreshTimer) window.clearTimeout(refreshTimer);
    if (snapshot?.stages.some((stage) => stage.status === 'running')) refreshTimer = window.setTimeout(load, 1000);
  });
</script>

<div class="mx-auto max-w-6xl">
  <header class="mb-6 flex flex-wrap items-end justify-between gap-6">
    <div><div class="eyebrow mb-2">Resolved outcome</div><p class="muted max-w-2xl">Run a ready transformation independently. The primary generation action follows the revisioned outcome plan and reuses matching completed artifacts.</p></div>
    <div class="flex flex-wrap gap-2"><button onclick={() => workflowTour=true} class="lift flex items-center gap-2 rounded-xl border border-[var(--line)] bg-[var(--paper-strong)] px-4 py-3 text-sm font-semibold"><Sparkles size={17}/> Tour</button>{#if snapshot?.sources.find((item) => item.filename.toLowerCase().endsWith('.pdf'))}{@const availablePdf = snapshot.sources.find((item) => item.filename.toLowerCase().endsWith('.pdf'))!}<button onclick={() => pdfSource=availablePdf} class="lift flex items-center gap-3 rounded-xl border border-[var(--line)] bg-[var(--paper-strong)] px-4 py-3 text-sm font-semibold"><Crop size={18}/> Edit PDF</button>{/if}<label class="lift flex cursor-pointer items-center gap-3 rounded-xl border border-[var(--line)] bg-[var(--paper-strong)] px-4 py-3 text-sm font-semibold">
      {#if uploading}<LoaderCircle class="animate-spin" size={18}/>{:else}<FileUp size={18}/>{/if}
      {uploading ? 'Importing…' : 'Add source'}
      <input type="file" class="sr-only" onchange={upload} disabled={uploading}/>
    </label></div>
  </header>
  {#if outcome?.pipeline}<div class="mb-6 flex flex-wrap items-center gap-2 rounded-2xl border border-[var(--line)] bg-[var(--paper-strong)] p-4">{#each outcome.pipeline as stage,index}<span class="rounded-lg bg-[var(--accent-soft)] px-3 py-2 text-xs font-semibold">{stage.title}</span>{#if index<outcome.pipeline.length-1}<ChevronRight class="muted" size={14}/>{/if}{/each}</div>{/if}

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
              <div class="min-w-0"><div class="flex flex-wrap items-center gap-2"><h2 class="text-lg font-semibold">{stage.title}</h2><span class:running={stage.status === 'running'} class:done={stage.status === 'completed'} class:warning={stage.status === 'stale' || stage.status === 'failed'} class="status-chip inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[.68rem] font-bold uppercase tracking-wider"><StatusIcon class={stage.status === 'running' ? 'animate-spin' : ''} size={12}/>{stage.status}</span></div><p class="muted mt-1.5 max-w-2xl text-sm leading-relaxed">{stage.explanation}</p>{#if stage.status==='running' && stage.progress!=null}<div class="mt-3 h-1.5 max-w-md overflow-hidden rounded-full bg-[var(--line)]"><div class="h-full bg-[var(--accent)]" style={`width:${Math.max(2,stage.progress*100)}%`}></div></div>{/if}{#if stage.detail}<p class="mt-2 text-xs text-red-500">{stage.detail}</p>{/if}{#if stage.artifact}<button onclick={() => previewArtifact(stage)} class="mt-2 flex items-center gap-1 text-xs font-semibold text-[var(--accent)]">Preview latest: {stage.artifact.role}<ChevronRight size={13}/></button>{/if}</div>
            </div>
            <div class="flex flex-wrap items-center gap-2 lg:justify-end">
              {#if stage.executable}
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
    <div class="surface max-h-[92vh] w-full max-w-xl overflow-y-auto rounded-[1.7rem] p-7" role="dialog" aria-modal="true" aria-labelledby="settings-title">
      <div class="flex justify-between gap-5"><div><div class="eyebrow">Stage settings</div><h2 id="settings-title" class="mt-1 text-2xl font-semibold">{settingsStage.title}</h2></div><button onclick={() => settingsStage=null} class="rounded-lg p-2"><X size={19}/></button></div>
      <div class="mt-6 grid gap-5">
        {#if ['correct','translate','optimize_tts','clean_source'].includes(settingsStage.key)}<label class="text-sm font-semibold">Model<input bind:value={model} placeholder="Use configured default" class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"/></label>{/if}
        {#if settingsStage.key === 'transcribe'}
          <label class="text-sm font-semibold">Recognition model<select bind:value={sttEngine} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"><option value="whisper">Whisper large-v3 · DTW timestamps</option><option value="parakeet">Parakeet TDT 0.6B v3 · native timestamps</option></select></label>
          <label class="text-sm font-semibold">Model precision<select bind:value={sttQuantization} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"><option value="f16">Full F16</option>{#if sttEngine === 'whisper'}<option value="q5_0">Q5_0 · 1.08 GB</option>{:else}<option value="q8_0">Q8_0 · 745 MB</option><option value="q5_0">Q5_0 · 541 MB</option><option value="q4_k">Q4_K · 489 MB</option>{/if}</select><span class="muted mt-1 block text-xs">F16 maximizes fidelity; quantized files reduce download and memory use.</span></label>
          <div class="grid gap-3 sm:grid-cols-[1fr_7rem]"><label class="text-sm font-semibold">Compute backend<select bind:value={sttComputeBackend} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"><option value="auto">Automatic</option><option value="cpu" disabled={!supportsSttCompute('cpu')}>CPU</option><option value="cuda" disabled={!supportsSttCompute('cuda')}>CUDA</option><option value="vulkan" disabled={!supportsSttCompute('vulkan')}>Vulkan</option><option value="metal" disabled={!supportsSttCompute('metal')}>Metal</option></select><span class="muted mt-1 block text-xs">Only backends compiled into the installed CrispASR runtime can be forced.</span></label><label class="text-sm font-semibold">Device<input type="number" min="0" disabled={['auto','cpu'].includes(sttComputeBackend)} bind:value={sttDevice} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal disabled:opacity-40"/></label></div>
          <div class="grid gap-3 sm:grid-cols-2"><label class="text-sm font-semibold">Source language<select bind:value={originalLanguage} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal">{#each LANGUAGE_OPTIONS as item}<option value={item.value}>{item.label}</option>{/each}</select></label><label class="text-sm font-semibold">Language detector<select bind:value={sttLidBackend} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"><option value="whisper">Whisper tiny</option><option value="ecapa">ECAPA (recommended)</option><option value="silero">Silero</option><option value="off">Off</option></select></label></div>
          <label class="flex items-center gap-3 text-sm font-semibold"><input type="checkbox" bind:checked={vadEnabled} class="size-4 accent-[var(--accent)]"/> Voice activity detection</label>
          {#if vadEnabled}<div class="grid grid-cols-2 gap-3"><label class="text-xs font-semibold">VAD model<select bind:value={vadModel} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"><option value="silero">Silero · general purpose</option><option value="firered">FireRedVAD · robust</option><option value="marblenet">MarbleNet · compact</option><option value="whisper-vad">Whisper VAD · experimental</option></select></label><label class="text-xs font-semibold">VAD threshold<span class="mt-1 grid min-h-10 grid-cols-[1fr_2.5rem] items-center gap-2 rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3"><input type="range" min="0" max="1" step="0.05" bind:value={vadThreshold} class="w-full accent-[var(--accent)]"/><output class="text-right text-xs font-bold">{Number(vadThreshold).toFixed(2)}</output></span></label><label class="text-xs font-semibold">Minimum speech (ms)<input type="number" min="0" bind:value={vadMinSpeech} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"/></label><label class="text-xs font-semibold">Minimum silence (ms)<input type="number" min="0" bind:value={vadMinSilence} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"/></label><label class="text-xs font-semibold">Maximum speech (s)<input type="number" min="1" bind:value={vadMaxSpeech} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"/></label><label class="text-xs font-semibold">Speech padding (ms)<input type="number" min="0" bind:value={vadSpeechPad} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"/></label></div>{/if}
          <details class="rounded-xl border border-[var(--line)] p-4"><summary class="cursor-pointer text-sm font-semibold">Decoder and long-form controls</summary><div class="mt-4 grid grid-cols-2 gap-3"><label class="text-xs font-semibold">Threads (0 = automatic)<input type="number" min="0" bind:value={sttThreads} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"/></label><label class="text-xs font-semibold">Beam size<input type="number" min="1" max="16" bind:value={sttBeamSize} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"/></label>{#if sttEngine === 'parakeet'}<label class="text-xs font-semibold">Parakeet decoder<select bind:value={parakeetDecoder} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"><option value="tdt">TDT greedy / beam</option><option value="maes">MAES beam</option><option value="ctc">CTC greedy</option></select></label>{/if}<label class="text-xs font-semibold">Forced chunk size (s, 0 = default)<input type="number" min="0" step="1" bind:value={sttChunkSeconds} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"/></label><label class="text-xs font-semibold">Chunk overlap (s)<input type="number" min="0" step="0.5" bind:value={sttChunkOverlap} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"/></label><label class="col-span-2 text-xs font-semibold">Hotwords<textarea rows="2" bind:value={sttHotwords} placeholder="Names and terminology, comma-separated" class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"></textarea></label></div><p class="muted mt-3 text-xs">Parakeet normally preserves full context and handles long recordings internally. Force chunking only for constrained systems or diagnostics.</p></details>
          <div class="rounded-xl border border-[var(--line)] p-4"><div class="text-sm font-semibold">Readable subtitle composition</div><p class="muted mt-1 text-xs">Independent from speech blocks and TTS segmentation. Defaults allow 48 characters per line for meetings while retaining two-line, 20 CPS and 0.833–7 second delivery guidance.</p><div class="mt-3 grid grid-cols-2 gap-3"><label class="text-xs font-semibold">Characters / line<input type="number" min="20" max="100" bind:value={subtitleChars} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"/></label><label class="text-xs font-semibold">Lines<input type="number" min="1" max="3" bind:value={subtitleLines} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"/></label><label class="text-xs font-semibold">Minimum duration (ms)<input type="number" min="250" bind:value={subtitleMinDuration} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"/></label><label class="text-xs font-semibold">Maximum duration (ms)<input type="number" min="1000" bind:value={subtitleMaxDuration} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"/></label><label class="text-xs font-semibold">Characters / second<input type="number" min="5" max="40" step="0.5" bind:value={subtitleCps} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"/></label><label class="text-xs font-semibold">Minimum cue gap (ms)<input type="number" min="0" max="500" bind:value={subtitleMinGap} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"/></label><label class="text-xs font-semibold">Phrase-break silence (ms)<input type="number" min="100" max="3000" bind:value={subtitlePhraseGap} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"/></label></div></div>
        {/if}
        {#if settingsStage.key === 'correct'}<label class="text-sm font-semibold">Correction guidance<textarea bind:value={instructions} rows="4" class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"></textarea></label>{/if}
        {#if settingsStage.key === 'translate'}<label class="text-sm font-semibold">Translation backend<select bind:value={backend} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"><option value="llm">LLM</option><option value="deepl">DeepL</option></select></label><label class="text-sm font-semibold">Target language<select bind:value={targetLanguage} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal">{#each LANGUAGE_OPTIONS.filter((item) => item.value !== 'auto') as item}<option value={item.value}>{item.label}</option>{/each}</select></label><label class="text-sm font-semibold">Translation guidance<textarea bind:value={instructions} rows="3" class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"></textarea></label>{/if}
        {#if settingsStage.key === 'optimize_tts'}<div class="rounded-xl border border-[var(--line)] bg-[var(--accent-soft)] p-4"><div class="text-sm font-semibold">Optional paid LLM pass</div><p class="muted mt-1 text-xs leading-relaxed">This is separate from subtitle correction. It expands pronunciation-sensitive numerals, abbreviations, names, and extraction artifacts for speech only. The result is saved as a before/after revision before TTS.</p></div><label class="text-sm font-semibold">Concurrent calls<input type="number" min="1" max="16" bind:value={optimizationConcurrent} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"/></label><label class="flex items-start gap-3 rounded-xl border border-[var(--line)] p-4"><input type="checkbox" bind:checked={optimizationMultiStage} class="mt-1 size-4 accent-[var(--accent)]"/><span><span class="block text-sm font-semibold">Three-stage prompts</span><span class="muted mt-1 block text-xs">Each non-empty prompt runs sequentially and incurs a separate provider request.</span></span></label>{#if optimizationMultiStage}<label class="text-sm font-semibold">First prompt<textarea bind:value={optimizationFirstPrompt} rows="3" class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"></textarea></label><label class="text-sm font-semibold">Second prompt<textarea bind:value={optimizationSecondPrompt} rows="3" class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"></textarea></label><label class="text-sm font-semibold">Third prompt<textarea bind:value={optimizationThirdPrompt} rows="3" class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"></textarea></label>{:else}<label class="text-sm font-semibold">Optimization prompt<textarea bind:value={optimizationPrompt} rows="5" placeholder="Leave blank for Pandrator's safe default" class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"></textarea></label>{/if}{/if}
        {#if settingsStage.key === 'clean_source'}<label class="flex items-start gap-3 rounded-xl border border-[var(--line)] p-4"><input type="checkbox" bind:checked={agentic} class="mt-1 size-4 accent-[var(--accent)]"/><span><span class="block text-sm font-semibold">Agentic review loop</span><span class="muted mt-1 block text-xs">Runs focused metadata, navigation, boilerplate, repeated-element, and chapter passes. Provider costs may apply.</span></span></label>{#if agentic}<label class="text-sm font-semibold">Maximum LLM turns<input type="number" min="5" max="500" bind:value={maxIterations} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"/></label>{/if}{/if}
        {#if ['prepare_text','generate_audio'].includes(settingsStage.key)}<label class="text-sm font-semibold">TTS service<select bind:value={ttsService} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal">{#each ttsCatalogue.services??[] as service}<option value={service.id}>{service.name}</option>{/each}</select></label><div class="flex items-center justify-between gap-3"><p class="muted text-xs">Models and voices come from the configured service catalogue.</p><button type="button" onclick={() => ttsServicesOpen = true} class="text-xs font-semibold text-[var(--accent)]">Manage services and voices</button></div><label class="text-sm font-semibold">Model<input list="tts-model-catalogue" bind:value={ttsModel} placeholder={selectedTtsService?.default_model??'Service default'} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"/><datalist id="tts-model-catalogue">{#each ttsModels as item}<option value={item}></option>{/each}</datalist></label><label class="text-sm font-semibold">Voice<input list="tts-voice-catalogue" bind:value={voiceName} placeholder={selectedTtsDefaultVoice||'Provider or library voice'} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"/><datalist id="tts-voice-catalogue">{#each ttsVoices as item}<option value={item}></option>{/each}</datalist></label><label class="text-sm font-semibold">Language<input bind:value={targetLanguage} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"/></label>{#if settingsStage.key === 'generate_audio'}<div class="rounded-xl border border-[var(--line)] p-4"><div class="text-sm font-semibold">Speech blocks for dubbing</div><p class="muted mt-1 text-xs">These TTS chunks are independent from the final subtitle layout.</p><div class="mt-3 grid grid-cols-2 gap-3"><label class="text-xs font-semibold">Minimum characters<input type="number" min="1" bind:value={speechBlockMinChars} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"/></label><label class="text-xs font-semibold">Maximum characters<input type="number" min="1" bind:value={speechBlockMaxChars} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"/></label><label class="text-xs font-semibold">Merge gap (ms)<input type="number" min="0" bind:value={speechBlockMergeThreshold} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"/></label></div></div>{/if}{/if}
        {#if settingsStage.key === 'export'}<label class="text-sm font-semibold">Audio<select bind:value={audioMode} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"><option value="preserve">Preserve source audio</option><option value="mixed">Mix source and dubbing</option><option value="dubbing_only">Dubbing only</option></select></label><label class="text-sm font-semibold">Subtitles<select bind:value={subtitleMode} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"><option value="none">None</option><option value="soft">Injected soft tracks / separate files</option><option value="burned">Burned subtitles</option></select></label>{#if subtitleMode !== 'none'}<label class="text-sm font-semibold">Subtitle tracks<select bind:value={subtitleSelection} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"><option value="source">Source / corrected</option><option value="translation">Translation</option><option value="dual">Source and translation</option></select></label>{/if}<div class="rounded-xl border border-[var(--line)] p-4"><div class="text-sm font-semibold">Final subtitle layout</div><p class="muted mt-1 text-xs">Applied to derived export subtitles only; dubbing speech blocks are unchanged.</p><div class="mt-3 grid grid-cols-2 gap-3"><label class="text-xs font-semibold">Characters / line<input type="number" min="20" max="100" bind:value={subtitleChars} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"/></label><label class="text-xs font-semibold">Lines<input type="number" min="1" max="3" bind:value={subtitleLines} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"/></label><label class="text-xs font-semibold">Minimum duration (ms)<input type="number" min="250" bind:value={subtitleMinDuration} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"/></label><label class="text-xs font-semibold">Maximum duration (ms)<input type="number" min="1000" bind:value={subtitleMaxDuration} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"/></label><label class="text-xs font-semibold">Characters / second<input type="number" min="5" max="40" step="0.5" bind:value={subtitleCps} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"/></label><label class="text-xs font-semibold">Minimum cue gap (ms)<input type="number" min="0" max="500" bind:value={subtitleMinGap} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"/></label><label class="text-xs font-semibold">Phrase-break silence (ms)<input type="number" min="100" max="3000" bind:value={subtitlePhraseGap} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"/></label></div></div>{/if}
      </div>
      <div class="mt-7 flex flex-wrap justify-end gap-3"><button onclick={() => { fullSettingsSection=stageSection(settingsStage!.key); settingsStage=null; }} class="mr-auto rounded-xl border border-[var(--line)] px-4 py-2.5 text-sm font-semibold">All {sectionDisplay(stageSection(settingsStage.key))} settings</button><button onclick={() => settingsStage=null} class="rounded-xl border border-[var(--line)] px-4 py-2.5 text-sm font-semibold">Cancel</button><button onclick={saveSettings} class="rounded-xl bg-[var(--accent)] px-4 py-2.5 text-sm font-semibold text-white">Save settings</button></div>
    </div>
  </div>
{/if}

{#if pdfSource}<PdfEditor sessionId={session.id} source={pdfSource} onclose={() => pdfSource=null}/>{/if}
{#if reviewOpen}<SubtitleReview sessionId={session.id} sourceArtifactId={snapshot?.sources[0]?.id} onclose={() => reviewOpen=false} onsaved={load}/>{/if}
{#if preview}<ArtifactPreview artifact={preview} onclose={()=>preview=null}/>{/if}
{#if fullSettingsSection}<SettingsModal sessionId={session.id} section={fullSettingsSection} title={`${sectionDisplay(fullSettingsSection)} settings`} description="These settings are saved as session overrides and inherited by future runs." onclose={()=>fullSettingsSection=''}/>{/if}
{#if ttsServicesOpen}<TtsServicesModal onclose={() => ttsServicesOpen=false}/>{/if}
<GuidedTour tourId="workflow" steps={workflowTourSteps} bind:open={workflowTour}/>

<style>
  .status-chip { color: var(--muted); background: color-mix(in srgb, var(--muted) 10%, transparent); }
  .status-chip.done { color: var(--success); background: color-mix(in srgb, var(--success) 12%, transparent); }
  .status-chip.running { color: var(--accent); background: var(--accent-soft); }
  .status-chip.warning { color: var(--warning); background: color-mix(in srgb, var(--warning) 12%, transparent); }
</style>
