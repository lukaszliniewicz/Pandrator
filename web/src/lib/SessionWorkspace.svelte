<script lang="ts">
  import {
    ArrowLeft,
    Check,
    ChevronRight,
    CircleAlert,
    Clock3,
    Crop,
    LoaderCircle,
    Library,
    Play,
    Plus,
    RefreshCw,
    RotateCcw,
    Save,
    Settings2,
    Sparkles,
    X
  } from '@lucide/svelte';
  import { api, type JobRecord, type SessionRecord } from './api';
  import SubtitleReview from './SubtitleReview.svelte';
  import GuidedTour from './GuidedTour.svelte';
  import ArtifactPreview from './ArtifactPreview.svelte';
  import SettingsModal from './SettingsModal.svelte';
  import TtsServicesModal from './TtsServicesModal.svelte';
  import VoiceLibraryModal from './VoiceLibraryModal.svelte';
  import TextOptimizationReview from './TextOptimizationReview.svelte';
  import AddSourceDialog from './AddSourceDialog.svelte';
  import StageArtifactHistory from './StageArtifactHistory.svelte';
  import { artifactRoleLabel, type PreviewableArtifact } from './artifact-display';
  import type { StageArtifact } from './stage-artifacts';
  import { LANGUAGE_OPTIONS } from './settings-fields';
  import { describeVoice, languagesForService } from './voice-catalog';
  import { onMount } from 'svelte';

  type Stage = {
    number: number;
    key: string;
    title: string;
    explanation: string;
    status: 'unavailable' | 'ready' | 'running' | 'completed' | 'stale' | 'failed';
    executable: boolean;
    toggle?: boolean;
    toggle_only?: boolean;
    enabled?: boolean | null;
    optimization_timing?: 'document' | 'generation';
    included: boolean;
    required?: boolean;
    artifact?: PreviewableArtifact & { id: string; role: string; raw_role?: string; path: string } | null;
    artifacts?: StageArtifact[];
    selected_artifact_id?: string | null;
    selection_revision?: number;
    job_id?: string | null;
    progress?: number | null;
    detail?: string | null;
    usage?: {
      input_tokens: number;
      cached_input_tokens: number;
      output_tokens: number;
      total_tokens: number;
      cost_usd: number | null;
      model_id: string;
      created_at: string;
    } | null;
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
  let llmModels = $state<{value:string;label:string;isDefault:boolean}[]>([]);
  let loading = $state(true);
  let error = $state('');
  let sourceDialog = $state(false);
  let sourceMessage = $state('');
  let pendingRun = $state<{stage: Stage; impact: any} | null>(null);
  let pendingSettingsMismatch = $state<{stage: Stage; mismatches: {stage: string; changed_fields: string[]}[]} | null>(null);
  const mismatchFieldLabel = (field: string) => ({backend: 'backend', target_language: 'target language', model: 'model', instructions: 'guidance'}[field] ?? field);
  const mismatchStageLabel = (key: string) => (key === 'translate' ? 'Translation' : 'Correction');
  let settingsStage = $state<Stage | null>(null);
  let stageMessage = $state('');
  let fullSettingsSection = $state('');
  let ttsServicesOpen = $state(false);
  let voiceLibraryOpen = $state(false);
  let voiceLibraryView = $state<'references' | 'prebuilt'>('references');
  let voiceLibraryService = $state('');
  let optimizationReviewArtifactId = $state('');
  let workspaceMode = $state<'review' | 'automatic'>('review');
  let preview = $state<PreviewableArtifact | null>(null);
  const sectionDisplay = (section: string) => ({ stt: 'STT', tts: 'TTS', rvc: 'RVC' } as Record<string, string>)[section] ?? section.replaceAll('_', ' ');
  const formatCost = (cost: number | null) => cost == null ? 'Cost not reported' : cost === 0 ? '$0.00' : cost < 0.01 ? `$${cost.toFixed(6)}` : `$${cost.toFixed(4)}`;
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
  let mossMaxChunkSeconds = $state(120);
  let mossVadEnabled = $state(false);
  let mossCtcAlignmentEnabled = $state(true);
  let mossCtcPaddingSeconds = $state(0.5);
  let vadEnabled = $state(true);
  let vadModel = $state('silero');
  let vadThreshold = $state(0.5);
  let vadMinSpeech = $state(250);
  let vadMinSilence = $state(800);
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
  let optimizationBatchSize = $state(3);
  let documentOptimizationBatchSize = $state(8);
  let optimizationMultiStage = $state(false);
  let optimizationFirstPrompt = $state('');
  let optimizationSecondPrompt = $state('');
  let optimizationThirdPrompt = $state('');
  let optimizationEnabled = $state(false);
  let documentOptimizationEnabled = $state(false);
  let optimizationTiming = $state<'document' | 'generation'>('generation');
  let agentic = $state(false);
  let maxIterations = $state(53);
  let splitSentences = $state(true);
  let appendSentences = $state(true);
  let maxSentenceLength = $state(200);
  let nemoNormalization = $state(true);
  let normalizeAllCaps = $state(true);
  let removeDiacritics = $state(false);
  let removeQuotationMarks = $state(false);
  let ttsService = $state('XTTS');
  let ttsModel = $state('');
  let voiceName = $state('');
  let generationPrompt = $state('');
  let speechBlockMinChars = $state(10);
  let speechBlockMaxChars = $state(220);
  let speechBlockMergeThreshold = $state(250);
  let subtitleMode = $state('soft');
  let subtitleSelection = $state('dual');
  let audioMode = $state('mixed');
  let exportMode = $state('media');
  let subtitleFormat = $state('srt');
  let pdfSource = $state<{ id: string; filename: string } | null>(null);
  let PdfEditorComponent = $state<(typeof import('./PdfEditor.svelte'))['default'] | null>(null);
  let reviewOpen = $state(false);
  let refreshingTtsServices = $state(false);
  let workflowTour = $state(false);
  const workflowTourSteps = [
    {section:'Workflow',title:'Stages are independent',body:'Run any ready card on its own. Its latest artifact, settings, and status stay attached to that stage.'},
    {section:'Workflow',title:'The outcome composes the pipeline',body:'Customize Workflow chooses meaningful transformations and deliverables. Run Now remains available on every ready transformation.'},
    {section:'Review',title:'Preview before synthesis',body:'Subtitle comparison aligns transcription, correction, and translation, including split and merged lineage. Saving creates a reviewed revision.'},
    {section:'Export',title:'Export does not require dubbing',body:'Subtitle-only exports preserve source audio. When dubbing exists, choose source, mixed, or dubbing-only audio and soft or burned subtitles.'}
  ];

  async function load(options: { initial?: boolean } = {}) {
    const initial = options.initial ?? snapshot === null;
    if (initial) loading = true;
    try {
      [snapshot, outcome] = await Promise.all([api<Snapshot>(`/sessions/${session.id}/workflow`), api(`/sessions/${session.id}/outcome-plan`)]);
      for (const stage of snapshot?.stages ?? []) {
        if (stage.artifact) {
          stage.artifact.raw_role = stage.artifact.role;
          stage.artifact.role = artifactRoleLabel(stage.artifact.role);
        }
      }
      const speechOptimization=snapshot?.stages.find((stage)=>stage.key==='optimize_tts');
      if(speechOptimization){
        optimizationTiming=speechOptimization.optimization_timing??'generation';
        documentOptimizationEnabled=Boolean(speechOptimization.enabled&&optimizationTiming==='document');
        optimizationEnabled=Boolean(speechOptimization.enabled&&optimizationTiming==='generation');
      }
    } catch (caught) {
      error = caught instanceof Error ? caught.message : String(caught);
    } finally {
      if (initial) loading = false;
    }
  }

  async function loadCapabilities() {
    try { capabilities = await api<Record<string, any>>('/capabilities'); }
    catch { capabilities = {}; }
  }

  const supportsSttCompute = (name: string) => name === 'auto' || (capabilities?.stt?.compute_backends ?? []).includes(name);
  const normalizeSttEngine = (value: unknown) => {
    const normalized=String(value??'').toLowerCase();
    if(normalized.includes('moss')) return 'moss';
    return normalized.includes('parakeet')?'parakeet':'whisper';
  };
  const sttOptionLabel = (engineId:string,label:string,timing:string) => {
    const info=capabilities?.stt?.models?.[engineId]??{};
    const readiness=info.default?'default':info.installed?'ready':'downloads on first use';
    return `${label} · ${timing} · ${readiness}`;
  };

  async function run(stage: Stage, confirmed = false, reuseStages: string[] = []) {
    if (stage.key === 'preview') {
      reviewOpen = true;
      return;
    }
    if (stage.key === 'export') {
      location.href = `/sessions/${session.id}/output`;
      return;
    }
    if (!confirmed && stage.artifact && (stage.artifacts?.length ?? 0) > 0) {
      try {
        const impact = await api<any>(`/sessions/${session.id}/stages/${stage.key}/impact`);
        pendingRun = { stage, impact };
      } catch (caught) {
        error = caught instanceof Error ? caught.message : String(caught);
      }
      return;
    }
    if (!confirmed && stage.key === 'generate_audio') {
      try {
        const preflight = await api<any>(`/sessions/${session.id}/stages/${stage.key}/settings-mismatches`);
        if ((preflight?.mismatches ?? []).length) {
          pendingSettingsMismatch = { stage, mismatches: preflight.mismatches };
          return;
        }
      } catch { /* the settings check is advisory; continue with the run */ }
    }
    error = '';
    try {
      const routeKey = stage.key === 'optimize_tts' && documentOptimizationEnabled ? 'optimize_document' : stage.key;
      const body = stage.key === 'generate_audio'
        ? { ...(stageSettings[stage.key] ?? {}), stage_settings: stageSettings, ...(reuseStages.length ? { reuse_stages: reuseStages } : {}) }
        : (stageSettings[stage.key] ?? {});
      await api<JobRecord>(`/sessions/${session.id}/stages/${routeKey}/run`, {
        method: 'POST',
        body: JSON.stringify(body)
      });
      await load();
    } catch (caught) {
      error = caught instanceof Error ? caught.message : String(caught);
    }
  }

  async function sourceAdded(message: string) { sourceMessage = message; await load({ initial: false }); }

  async function openPdfEditor(source: { id: string; filename: string }) {
    PdfEditorComponent ??= (await import('./PdfEditor.svelte')).default;
    pdfSource = source;
  }

  async function chooseStageArtifact(stage: Stage, artifactId: string) {
    if (!artifactId || artifactId === stage.selected_artifact_id) return;
    error = '';
    try {
      await api(`/sessions/${session.id}/stages/${stage.key}/selection`, {
        method: 'PUT',
        headers: { 'If-Match': `"${stage.selection_revision ?? 0}"` },
        body: JSON.stringify({ artifact_id: artifactId })
      });
      await load({ initial: false });
    } catch (caught) { error = caught instanceof Error ? caught.message : String(caught); }
  }

  async function clearStageArtifact(stage: Stage) {
    if (!stage.selected_artifact_id || !confirm(`Clear the selected ${stage.title.toLowerCase()} result? Dependent stage selections will also be cleared, but every artifact remains in history.`)) return;
    error = '';
    try {
      await api(`/sessions/${session.id}/stages/${stage.key}/selection`, {
        method: 'PUT',
        headers: { 'If-Match': `"${stage.selection_revision ?? 0}"` },
        body: JSON.stringify({ artifact_id: null })
      });
      await load({ initial: false });
    } catch (caught) { error = caught instanceof Error ? caught.message : String(caught); }
  }

  const stageSection = (key: string) => ({transcribe:'stt',correct:'correction',translate:'translation',optimize_document:'text',optimize_tts:'text',clean_source:'source_cleaning',prepare_text:'text',generate_audio:'tts',export:'output'}[key] ?? 'text');

  async function openSettings(stage: Stage) {
    if (stage.key === 'export' && session.workflow_kind === 'audiobook') {
      fullSettingsSection = 'output';
      return;
    }
    settingsStage = stage;
    stageMessage = '';
    let saved = stageSettings[stage.key] ?? {};
    let storedSettings:any = null;
    try {
      const stored = await api<any>(`/sessions/${session.id}/settings/${stageSection(stage.key)}`);
      storedSettings = stored;
      saved = {...stored.effective, ...saved};
      stageSettings[stage.key] = saved;
    } catch { /* use stage-local values */ }
    targetLanguage = String((stage.key === 'generate_audio' ? saved.language : saved.target_language) ?? session.target_language ?? (session.source_language === 'auto' ? 'en' : session.source_language) ?? 'en');
    originalLanguage = String(saved.original_language ?? session.source_language ?? 'auto');
    model = String(saved.model_name ?? saved.tts_optimization_model ?? saved[`${stage.key}_model`] ?? 'default');
    backend = String(saved.backend ?? saved.translation_backend ?? 'llm');
    const hasSavedSttModel = Boolean(storedSettings?.override?.stt_engine || storedSettings?.global?.stt_engine || stageSettings[stage.key]?.stt_engine);
    const preferredSttEngine = String(capabilities?.stt?.default_engine ?? 'whisper');
    sttEngine = normalizeSttEngine(hasSavedSttModel ? (saved.stt_engine ?? saved.stt_backend) : preferredSttEngine);
    sttQuantization = String(hasSavedSttModel ? (saved.stt_model_quantization ?? capabilities?.stt?.models?.[sttEngine]?.precision ?? 'f16') : (capabilities?.stt?.default_model_quantization ?? 'f16'));
    sttComputeBackend = String(saved.stt_compute_backend ?? 'auto');
    sttDevice = Number(saved.stt_compute_device ?? 0);
    sttThreads = Number(saved.stt_threads ?? 0);
    sttChunkSeconds = Number(saved.stt_chunk_seconds ?? 0);
    sttChunkOverlap = Number(saved.stt_chunk_overlap_seconds ?? 3);
    sttHotwords = String(saved.stt_hotwords ?? '');
    sttLidBackend = String(saved.stt_lid_backend ?? 'whisper');
    sttBeamSize = Number(saved.stt_beam_size ?? 1);
    parakeetDecoder = String(saved.parakeet_decoder ?? 'tdt');
    mossMaxChunkSeconds = Number(saved.moss_max_chunk_seconds ?? 120);
    mossVadEnabled = Boolean(saved.moss_vad_enabled ?? false);
    mossCtcAlignmentEnabled = Boolean(saved.moss_ctc_alignment_enabled ?? true);
    mossCtcPaddingSeconds = Number(saved.moss_ctc_padding_seconds ?? 0.5);
    vadEnabled = Boolean(saved.crispasr_vad_enabled ?? true);
    vadModel = String(saved.crispasr_vad_model ?? 'silero');
    vadThreshold = Number(saved.crispasr_vad_threshold ?? 0.5);
    vadMinSpeech = Number(saved.crispasr_vad_min_speech_ms ?? 250);
    vadMinSilence = Number(saved.crispasr_vad_min_silence_ms ?? 800);
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
    optimizationBatchSize = Number(saved.llm_tts_batch_size ?? 3);
    documentOptimizationBatchSize = Number(saved.llm_tts_document_batch_size ?? 8);
    optimizationMultiStage = Boolean(saved.llm_multi_stage ?? false);
    optimizationFirstPrompt = String(saved.first_prompt ?? '');
    optimizationSecondPrompt = String(saved.second_prompt ?? '');
    optimizationThirdPrompt = String(saved.third_prompt ?? '');
    optimizationEnabled = Boolean(saved.llm_tts_optimization ?? (stage.key === 'optimize_tts' ? stage.enabled : false) ?? false);
    documentOptimizationEnabled = Boolean(saved.llm_tts_document_optimization ?? (stage.key === 'optimize_document' ? stage.enabled : false) ?? false);
    optimizationTiming = documentOptimizationEnabled ? 'document' : 'generation';
    agentic = Boolean(saved.agentic ?? false);
    maxIterations = Number(saved.max_iterations ?? 53);
    splitSentences = Boolean(saved.enable_sentence_splitting ?? true);
    appendSentences = Boolean(saved.enable_sentence_appending ?? true);
    maxSentenceLength = Number(saved.max_sentence_length ?? 200);
    nemoNormalization = Boolean(saved.enable_nemo_normalization ?? true);
    normalizeAllCaps = Boolean(saved.normalize_all_caps ?? true);
    removeDiacritics = Boolean(saved.remove_diacritics ?? false);
    removeQuotationMarks = Boolean(saved.remove_quotation_marks ?? false);
    const configuredServiceId=String(saved.tts_service ?? saved.service ?? ttsCatalogue.default_service ?? 'XTTS');
    const configuredService=(ttsCatalogue.services??[]).find((item:any)=>[item.id,item.name].some((value)=>String(value??'').toLowerCase()===configuredServiceId.toLowerCase()));
    const activeService=(configuredService?.online?configuredService:(ttsCatalogue.services??[]).find((item:any)=>item.online))??configuredService;
    ttsService = String(activeService?.id ?? configuredServiceId);
    ttsModel = activeService?.id===configuredService?.id ? String(saved.model ?? saved.xtts_model ?? activeService?.default_model ?? '') : String(activeService?.default_model ?? activeService?.models?.[0] ?? '');
    voiceName = activeService?.id===configuredService?.id ? String(saved.voice ?? saved.voice_name ?? '') : String(activeService?.default_voice ?? '');
    generationPrompt = String(saved.generation_prompt ?? '');
    speechBlockMinChars = Number(saved.speech_block_min_chars ?? 10);
    speechBlockMaxChars = Number(saved.speech_block_max_chars ?? 220);
    speechBlockMergeThreshold = Number(saved.speech_block_merge_threshold ?? 250);
    subtitleMode = String(saved.subtitle_mode ?? 'soft');
    subtitleSelection = String(saved.subtitle_selection ?? 'dual');
    audioMode = String(saved.audio_mode ?? (session.workflow_kind === 'voiceover' ? 'mixed' : 'preserve'));
    exportMode = String(saved.export_mode ?? (session.workflow_kind === 'subtitles' ? 'subtitles' : 'media'));
    if (session.workflow_kind === 'subtitles' && !['subtitles','text'].includes(exportMode)) exportMode = 'subtitles';
    subtitleFormat = String(saved.subtitle_format ?? 'srt');
    if (stage.key === 'generate_audio' && activeService) await discoverTtsService(activeService);
    if (stage.key === 'generate_audio' && selectedTtsService) {
      ttsService = String(selectedTtsService.id ?? selectedTtsService.name);
      ttsModel = ttsModel || String(selectedTtsService.default_model ?? ttsModels[0] ?? '');
      voiceName = voiceName || String(selectedTtsDefaultVoice ?? '');
      if (String(selectedTtsService.id).toLowerCase()==='kobold_qwen') {
        const catalogue=Array.from(selectedTtsService.voice_catalogues?.[ttsModel]??[]).map((voice:any)=>String(voice));
        const published=libraryVoices.flatMap((voice:any)=>{
          const registration=voice?.metadata_json?.providers?.kobold_qwen;
          return registration?.status==='ready'&&registration?.voice_id?[String(registration.voice_id)]:[];
        });
        const allowed=ttsModel.toLowerCase()==='voice cloning'?[...catalogue,...published]:catalogue;
        if (!allowed.some((voice:string)=>voice.toLowerCase()===voiceName.toLowerCase())) {
          voiceName=String(selectedTtsService.default_voices?.[ttsModel]??allowed[0]??'');
        }
      }
    }
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

  async function persistDefaultSection(section:string,value:Record<string,unknown>) {
    const defaults=await api<any>(`/defaults/${section}`);
    await api(`/settings/defaults.${section}`,{method:'PUT',headers:{'If-Match':`"${defaults.revision}"`},body:JSON.stringify({value:{...(defaults.value??{}),...value}})});
    const stored=await api<any>(`/sessions/${session.id}/settings/${section}`);
    const cleaned={...(stored.override??{})};
    for(const key of Object.keys(value)) delete cleaned[key];
    await api(`/sessions/${session.id}/settings/${section}`,{method:'PUT',headers:{'If-Match':`"${stored.revision}"`},body:JSON.stringify({value:cleaned})});
  }

  async function clearSectionOverrides(section:string,keys:string[]) {
    const stored=await api<any>(`/sessions/${session.id}/settings/${section}`);
    const cleaned={...(stored.override??{})};
    for(const key of keys) delete cleaned[key];
    await api(`/sessions/${session.id}/settings/${section}`,{method:'PUT',headers:{'If-Match':`"${stored.revision}"`},body:JSON.stringify({value:cleaned})});
  }

  async function loadSpeechCatalogues(preserveSelection = false) {
    const previousService = ttsService;
    const previousModel = ttsModel;
    const previousVoice = voiceName;
    try {
      const [services, voices] = await Promise.all([api<any>('/services/tts?refresh=true'), api<any>('/voices')]);
      ttsCatalogue = services;
      libraryVoices = voices.items ?? [];
      const catalogue = services.services ?? [];
      const configured = catalogue.find((item:any) => [item.id,item.name].some((value) => String(value ?? '').toLowerCase() === String(services.default_service ?? '').toLowerCase()));
      const preserved = catalogue.find((item:any) => [item.id,item.name].some((value) => String(value ?? '').toLowerCase() === previousService.toLowerCase()));
      const active = (preserveSelection ? preserved : null) ?? (configured?.online ? configured : catalogue.find((item:any) => item.online)) ?? configured ?? catalogue[0];
      if (active) {
        ttsService = String(active.id ?? active.name);
        ttsModel = preserveSelection && preserved ? previousModel : ttsModel || String(active.default_model ?? active.models?.[0] ?? '');
        voiceName = preserveSelection && preserved ? previousVoice : voiceName || String(active.default_voices_by_language?.[ttsModel]?.[targetLanguage] ?? active.default_voices?.[ttsModel] ?? active.default_voice ?? '');
        await discoverTtsService(active);
      }
    } catch {
      ttsCatalogue = {services:[]};
      libraryVoices = [];
    }
  }

  async function refreshSpeechServices() {
    refreshingTtsServices = true;
    error = '';
    try {
      await loadSpeechCatalogues(true);
    } finally {
      refreshingTtsServices = false;
    }
  }

  async function loadLlmModels() {
    try {
      const providerPayload=await api<any>('/providers');
      const enabled=(providerPayload.items??[]).filter((provider:any)=>provider.enabled);
      const groups=await Promise.all(enabled.map(async(provider:any)=>({provider,models:(await api<any>(`/providers/${provider.id}/models`)).items??[]})));
      llmModels=groups.flatMap(({provider,models}:any)=>models.filter((item:any)=>item.is_active).map((item:any)=>{
        const custom=Boolean(provider.options_json?.is_custom)||!['openai','gemini','anthropic'].includes(provider.provider_key);
        const providerId=custom?(provider.options_json?.provider_id||provider.id):(provider.options_json?.provider_id||provider.provider_key);
        return {value:custom?`custom:${providerId}/${item.model_id}`:`${provider.provider_key}/${item.model_id}`,label:`${provider.label} · ${item.model_id}`,isDefault:Boolean(item.is_default)};
      }));
    } catch { llmModels=[]; }
  }

  async function discoverTtsService(service:any = selectedTtsService) {
    if (!service?.api_base) return;
    try {
      const discovered = await api<any>('/services/tts/discover',{method:'POST',body:JSON.stringify({base_url:service.api_base})});
      if (!discovered?.success) return;
      const services = (ttsCatalogue.services??[]).map((item:any)=>item.id===service.id?{
        ...item,
        models:Array.from(new Set([...(discovered.models??[]),...(item.models??[])])),
        voices:Array.from(new Set([...(discovered.voices??[]),...(item.voices??[])])),
        live_voices:Array.from(new Set(discovered.voices??[])),
        online:true
      }:item);
      ttsCatalogue={...ttsCatalogue,services};
    } catch { /* A reachable service may not expose catalogue routes. */ }
  }

  async function chooseTtsService(value:string) {
    ttsService=value;
    const service=(ttsCatalogue.services??[]).find((item:any)=>String(item.id)===value);
    ttsModel=String(service?.default_model??service?.models?.[0]??'');
    await discoverTtsService(service);
    voiceName=String(service?.default_voices_by_language?.[ttsModel]?.[targetLanguage]??service?.default_voices?.[ttsModel]??service?.default_voice??'');
  }

  function chooseTtsModel(value:string) {
    ttsModel=value;
    const service=selectedTtsService;
    const modelVoices=service?.voice_catalogues?.[value]??[];
    voiceName=String(service?.default_voices_by_language?.[value]?.[targetLanguage]??service?.default_voices?.[value]??modelVoices[0]??'');
  }

  function openVoiceLibrary(view:'references'|'prebuilt', serviceId='') {
    voiceLibraryView=view;
    voiceLibraryService=serviceId;
    voiceLibraryOpen=true;
  }

  async function usePublishedVoice(providerVoiceId:string) {
    voiceName=providerVoiceId;
    voiceLibraryOpen=false;
    await loadSpeechCatalogues(true);
    voiceName=providerVoiceId;
  }

  const selectedTtsService = $derived((ttsCatalogue.services??[]).find((item:any)=>[item.id,item.name].map((value)=>String(value??'').toLowerCase()).includes(ttsService.toLowerCase())));
  const ttsModels = $derived(selectedTtsService?.models??[]);
  const selectedTtsDefaultVoice = $derived(selectedTtsService?.default_voices_by_language?.[ttsModel]?.[targetLanguage] ?? selectedTtsService?.default_voices?.[ttsModel] ?? selectedTtsService?.default_voice ?? '');
  const selectedTtsServiceId = $derived(String(selectedTtsService?.id??ttsService).toLowerCase());
  const generationPromptModels = $derived(Array.from(selectedTtsService?.generation_prompt_models??[]).map((model:any)=>String(model).toLowerCase()));
  const supportsGenerationPrompt = $derived(generationPromptModels.includes(ttsModel.toLowerCase()));
  const qwenVoiceCloning = $derived(selectedTtsServiceId==='kobold_qwen' && ttsModel.toLowerCase()==='voice cloning');
  const supportsCloningVoices = $derived(Boolean(selectedTtsService?.supports_voice_cloning));
  const supportsPrebuiltVoices = $derived(Boolean(selectedTtsService?.supports_prebuilt_voices && !qwenVoiceCloning));
  const selectedModelVoiceIds = $derived(selectedTtsService?.voice_catalogues?.[ttsModel] ?? (supportsPrebuiltVoices ? selectedTtsService?.voices ?? [] : []));
  const ttsVoiceDescriptors = $derived(Array.from(new Set(selectedModelVoiceIds)).map((voice)=>describeVoice(
    String(selectedTtsService?.id??ttsService),
    String(voice),
    selectedTtsService?.voice_metadata?.[`${ttsModel}:${String(voice)}`]
  )));
  const ttsLanguages = $derived(languagesForService(String(selectedTtsService?.id??ttsService),ttsVoiceDescriptors));
  const filteredPrebuiltVoices = $derived(ttsVoiceDescriptors.filter((voice)=>!voice.languageCode||voice.languageCode===targetLanguage));
  const publishedProviderVoices = $derived(libraryVoices.flatMap((voice:any)=>{
    const registration=voice?.metadata_json?.providers?.[selectedTtsServiceId];
    return registration?.status==='ready'&&registration?.voice_id?[String(registration.voice_id)]:[];
  }));
  const prebuiltVoiceIds = $derived(new Set(Array.from(selectedTtsService?.voice_catalogues?.['Prebuilt Voices']??[]).map((voice:any)=>String(voice).toLowerCase())));
  const clonedVoiceIds = $derived(Array.from(new Set([
    ...(qwenVoiceCloning?selectedModelVoiceIds:[]),
    ...(selectedTtsService?.live_voices??[]),
    ...publishedProviderVoices,
    ...(!selectedTtsService?.supports_prebuilt_voices?(selectedTtsService?.voices??[]):[])
  ].map((voice:any)=>String(voice)).filter((voice:string)=>voice&&!prebuiltVoiceIds.has(voice.toLowerCase())))));
  const clonedVoiceDescriptors = $derived(clonedVoiceIds.map((voice)=>describeVoice(selectedTtsServiceId,voice)));
  const showClonedVoices = $derived(Boolean(supportsCloningVoices && (!selectedTtsService?.supports_prebuilt_voices || qwenVoiceCloning)));

  async function toggleOptimization(enabled:boolean) {
    error='';
    try {
      await persistSection('text',{llm_tts_optimization:enabled,llm_processing_enabled:enabled});
      const current=await api<any>(`/sessions/${session.id}/outcome-plan`);
      const value={...current.value,transformations:{...(current.value?.transformations??{}),llm_tts_optimization:enabled}};
      await api(`/sessions/${session.id}/outcome-plan`,{method:'PUT',headers:{'If-Match':`"${current.revision}"`},body:JSON.stringify({value})});
      optimizationEnabled=enabled;
      await load();
    } catch(caught){error=caught instanceof Error?caught.message:String(caught)}
  }

  async function toggleDocumentOptimization(enabled:boolean) {
    error='';
    try {
      await persistSection('text',{llm_tts_document_optimization:enabled});
      const current=await api<any>(`/sessions/${session.id}/outcome-plan`);
      const value={...current.value,transformations:{...(current.value?.transformations??{}),llm_tts_document_optimization:enabled}};
      await api(`/sessions/${session.id}/outcome-plan`,{method:'PUT',headers:{'If-Match':`"${current.revision}"`},body:JSON.stringify({value})});
      documentOptimizationEnabled=enabled;
      await load();
    } catch(caught){error=caught instanceof Error?caught.message:String(caught)}
  }

  async function toggleSpeechOptimization(enabled:boolean) {
    error='';
    const documentEnabled=enabled&&optimizationTiming==='document';
    const generationEnabled=enabled&&optimizationTiming==='generation';
    try {
      await persistSection('text',{llm_tts_optimization:generationEnabled,llm_processing_enabled:generationEnabled,llm_tts_document_optimization:documentEnabled});
      const current=await api<any>(`/sessions/${session.id}/outcome-plan`);
      const value={...current.value,transformations:{...(current.value?.transformations??{}),llm_tts_optimization:generationEnabled,llm_tts_document_optimization:documentEnabled}};
      await api(`/sessions/${session.id}/outcome-plan`,{method:'PUT',headers:{'If-Match':`"${current.revision}"`},body:JSON.stringify({value})});
      optimizationEnabled=generationEnabled;
      documentOptimizationEnabled=documentEnabled;
      await load();
    } catch(caught){error=caught instanceof Error?caught.message:String(caught)}
  }

  function previewArtifact(stage: Stage) {
    if (!stage.artifact) return;
    const role = stage.artifact.raw_role ?? stage.artifact.role;
    if (role === 'tts_optimized' && stage.artifact.kind === 'json') {
      optimizationReviewArtifactId = stage.artifact.id;
      return;
    }
    if (['transcription','correction','translation','tts_optimized'].includes(role)) {
      reviewOpen = true;
      return;
    }
    preview = { ...stage.artifact, role, relative_path: stage.artifact.relative_path ?? stage.artifact.path };
  }

  function stageSectionUpdates(key:string):{section:string;value:Record<string,unknown>}[] {
    if(key==='transcribe') return [
      {section:'stt',value:stageSettings[key]},
      {section:'subtitles',value:{max_chars_per_line:subtitleChars,max_lines:subtitleLines,min_duration_ms:subtitleMinDuration,max_duration_ms:subtitleMaxDuration,max_cps:subtitleCps,min_gap_ms:subtitleMinGap,phrase_gap_ms:subtitlePhraseGap}}
    ];
    if(key==='correct') return [{section:'correction',value:{enabled:true,model_name:model==='default'?'':model,instructions}}];
    if(key==='translate') return [{section:'translation',value:{enabled:true,backend,target_language:targetLanguage,model_name:model==='default'?'':model,instructions}}];
    if(key==='optimize_tts') return [{section:'text',value:{llm_tts_optimization:optimizationEnabled,llm_processing_enabled:optimizationEnabled,llm_tts_document_optimization:documentOptimizationEnabled,tts_optimization_model:model==='default'?'':model,llm_tts_batch_size:optimizationBatchSize,llm_tts_document_batch_size:documentOptimizationBatchSize,llm_concurrent_calls:optimizationConcurrent,llm_multi_stage:optimizationMultiStage,combined_prompt:optimizationPrompt,first_prompt:optimizationFirstPrompt,second_prompt:optimizationSecondPrompt,third_prompt:optimizationThirdPrompt}}];
    if(key==='optimize_document') return [{section:'text',value:{llm_tts_document_optimization:documentOptimizationEnabled,tts_optimization_model:model==='default'?'':model,llm_tts_document_batch_size:documentOptimizationBatchSize,llm_concurrent_calls:optimizationConcurrent,llm_multi_stage:optimizationMultiStage,combined_prompt:optimizationPrompt,first_prompt:optimizationFirstPrompt,second_prompt:optimizationSecondPrompt,third_prompt:optimizationThirdPrompt}}];
    return [{section:stageSection(key),value:stageSettings[key]}];
  }

  async function revertStageToDefaults() {
    if(!settingsStage) return;
    const stage=settingsStage;
    const updates=stageSectionUpdates(stage.key);
    try {
      for(const update of updates) await clearSectionOverrides(update.section,Object.keys(update.value));
      const next={...stageSettings}; delete next[stage.key]; stageSettings=next;
      await openSettings(stage);
      stageMessage='Reverted to application defaults.';
    } catch(caught){error=caught instanceof Error?caught.message:String(caught)}
  }

  async function saveSettings(mode:'session'|'defaults'='session') {
    if (!settingsStage) return;
    const key = settingsStage.key;
    if (key === 'generate_audio' && showClonedVoices && (!voiceName || !clonedVoiceIds.some((voice)=>voice.toLowerCase()===voiceName.toLowerCase()))) {
      error='Choose a provider-ready cloned voice, or create and upload one through the Voice Library.';
      return;
    }
    const common = { model_name: model === 'default' ? '' : model, [`${key}_model`]: model };
    if (key === 'transcribe') stageSettings[key] = {
      stt_engine: sttEngine, stt_backend: sttEngine, stt_model_quantization: sttQuantization,
      stt_compute_backend: sttComputeBackend,
      stt_compute_device: sttDevice, stt_language: sttEngine === 'moss' ? 'auto' : originalLanguage, original_language: sttEngine === 'moss' ? 'auto' : originalLanguage,
      stt_threads: sttThreads, stt_chunk_seconds: sttEngine === 'moss' ? 0 : sttChunkSeconds,
      stt_chunk_overlap_seconds: sttChunkOverlap, stt_hotwords: sttHotwords,
      stt_lid_backend: sttLidBackend, stt_beam_size: sttBeamSize,
      parakeet_decoder: parakeetDecoder,
      moss_max_chunk_seconds: mossMaxChunkSeconds,
      moss_vad_enabled: mossVadEnabled,
      moss_ctc_alignment_enabled: mossCtcAlignmentEnabled,
      moss_ctc_aligner_model: 'auto',
      moss_ctc_padding_seconds: mossCtcPaddingSeconds,
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
    else if (key === 'optimize_tts') {
      const enabled=Boolean(settingsStage.enabled);
      optimizationEnabled=enabled&&optimizationTiming==='generation';
      documentOptimizationEnabled=enabled&&optimizationTiming==='document';
      stageSettings[key] = { ...common, llm_tts_optimization:optimizationEnabled, llm_tts_document_optimization:documentOptimizationEnabled, llm_tts_batch_size:optimizationTiming==='document'?documentOptimizationBatchSize:optimizationBatchSize, llm_tts_document_batch_size:documentOptimizationBatchSize, combined_prompt:optimizationPrompt, llm_concurrent_calls:optimizationConcurrent, llm_multi_stage:optimizationMultiStage, first_prompt:optimizationFirstPrompt, second_prompt:optimizationSecondPrompt, third_prompt:optimizationThirdPrompt };
    }
    else if (key === 'optimize_document') stageSettings[key] = { ...common, llm_tts_document_optimization:documentOptimizationEnabled, llm_tts_document_batch_size:documentOptimizationBatchSize, llm_tts_batch_size:documentOptimizationBatchSize, combined_prompt:optimizationPrompt, llm_concurrent_calls:optimizationConcurrent, llm_multi_stage:optimizationMultiStage, first_prompt:optimizationFirstPrompt, second_prompt:optimizationSecondPrompt, third_prompt:optimizationThirdPrompt };
    else if (key === 'clean_source') stageSettings[key] = { ...common, agentic, max_iterations: maxIterations };
    else if (key === 'prepare_text') stageSettings[key] = { enable_sentence_splitting:splitSentences, enable_sentence_appending:appendSentences, max_sentence_length:maxSentenceLength, enable_nemo_normalization:nemoNormalization, normalize_all_caps:normalizeAllCaps, remove_diacritics:removeDiacritics, remove_quotation_marks:removeQuotationMarks };
    else if (key === 'generate_audio') stageSettings[key] = {
      tts_service: ttsService, service: ttsService, model: ttsModel, xtts_model: ttsModel, voice: voiceName,
      generation_prompt: generationPrompt,
      language: targetLanguage, target_language: targetLanguage,
      speech_block_min_chars: speechBlockMinChars,
      speech_block_max_chars: speechBlockMaxChars,
      speech_block_merge_threshold: speechBlockMergeThreshold
    };
    else if (key === 'export') stageSettings[key] = {
      export_mode: exportMode, subtitle_format: subtitleFormat,
      subtitle_mode: subtitleMode, subtitle_selection: subtitleSelection, audio_mode: audioMode,
      subtitle_max_chars_per_line: subtitleChars, subtitle_max_lines: subtitleLines,
      subtitle_min_duration_ms: subtitleMinDuration, subtitle_max_duration_ms: subtitleMaxDuration,
      subtitle_max_cps: subtitleCps, subtitle_min_gap_ms: subtitleMinGap,
      subtitle_phrase_gap_ms: subtitlePhraseGap
    };
    else stageSettings[key] = common;
    const updates=stageSectionUpdates(key);
    try {
      if(mode==='defaults') {
        for(const update of updates) await persistDefaultSection(update.section,update.value);
        stageMessage='Saved as the application defaults for future sessions.';
      } else {
        for(const update of updates) await persistSection(update.section,update.value);
      }
      if (mode==='session' && key === 'optimize_tts') {
        const current=await api<any>(`/sessions/${session.id}/outcome-plan`);
        await api(`/sessions/${session.id}/outcome-plan`,{method:'PUT',headers:{'If-Match':`"${current.revision}"`},body:JSON.stringify({value:{...current.value,transformations:{...(current.value?.transformations??{}),llm_tts_optimization:optimizationEnabled,llm_tts_document_optimization:documentOptimizationEnabled}}})});
      }
      else if (mode==='session' && key === 'optimize_document') {
        const current=await api<any>(`/sessions/${session.id}/outcome-plan`);
        await api(`/sessions/${session.id}/outcome-plan`,{method:'PUT',headers:{'If-Match':`"${current.revision}"`},body:JSON.stringify({value:{...current.value,transformations:{...(current.value?.transformations??{}),llm_tts_document_optimization:documentOptimizationEnabled}}})});
      }
      if(mode==='session') {
        await load();
        settingsStage = null;
      }
    } catch (caught) { error=caught instanceof Error?caught.message:String(caught); }
  }

  const statusIcon = (status: Stage['status']) => {
    if (status === 'completed') return Check;
    if (status === 'running') return LoaderCircle;
    if (status === 'stale' || status === 'failed') return CircleAlert;
    return Clock3;
  };

  async function generateAutomatically() {
    const stage = snapshot?.stages.find((item) => item.key === 'generate_audio');
    if (!stage) return;
    try {
      const preflight = await api<any>(`/sessions/${session.id}/stages/generate_audio/settings-mismatches`);
      const mismatches = preflight?.mismatches ?? [];
      if (mismatches.length) {
        const names = mismatches.map((item: any) => (item.stage === 'translate' ? 'translation' : 'correction')).join(' and ');
        sourceMessage = `Reusing the existing ${names} even though its settings changed; run the stage manually to update it.`;
        await run(stage, true, mismatches.map((item: any) => String(item.stage)));
        return;
      }
    } catch { /* the settings check is advisory; continue with the run */ }
    await run(stage);
  }

  onMount(async () => {
    workspaceMode = localStorage.getItem(`pandrator:workspace-mode:${session.id}`) === 'automatic' ? 'automatic' : 'review';
    await Promise.all([load({ initial: true }), loadCapabilities(), loadSpeechCatalogues(), loadLlmModels()]);
  });
  onMount(() => {
    const refresh = () => load({ initial: false });
    window.addEventListener('pandrator:generation-changed', refresh);
    return () => window.removeEventListener('pandrator:generation-changed', refresh);
  });
  $effect(() => { if (typeof localStorage !== 'undefined') localStorage.setItem(`pandrator:workspace-mode:${session.id}`, workspaceMode); });
  $effect(() => {
    if (!snapshot?.stages.some((stage) => stage.status === 'running')) return;
    const timer = window.setTimeout(() => load({ initial: false }), 1000);
    return () => window.clearTimeout(timer);
  });
</script>

<div class="mx-auto max-w-6xl">
  <header class="mb-6 flex flex-wrap items-end justify-between gap-6">
    <div><div class="eyebrow mb-2">Resolved outcome</div><p class="muted max-w-2xl">{session.workflow_kind==='subtitles'?'Transcribe, refine, translate, and export subtitle documents. Voice generation and rendered video remain available by converting this workspace to voiceover.':'Choose how much control you want while keeping the same settings, artifacts, and review history.'}</p>{#if session.workflow_kind!=='subtitles'}<div class="mt-4 inline-flex rounded-xl border border-[var(--line)] bg-[var(--paper-strong)] p-1" aria-label="Workspace mode"><button onclick={()=>workspaceMode='review'} class:mode-active={workspaceMode==='review'} class="mode-choice">Review each stage</button><button onclick={()=>workspaceMode='automatic'} class:mode-active={workspaceMode==='automatic'} class="mode-choice">Generate automatically</button></div>{/if}</div>
    <div class="flex flex-wrap gap-2"><button onclick={() => workflowTour=true} class="lift flex items-center gap-2 rounded-xl border border-[var(--line)] bg-[var(--paper-strong)] px-4 py-3 text-sm font-semibold"><Sparkles size={17}/> Tour</button>{#if snapshot?.sources.find((item) => item.filename.toLowerCase().endsWith('.pdf'))}{@const availablePdf = snapshot.sources.find((item) => item.filename.toLowerCase().endsWith('.pdf'))!}<button onclick={() => openPdfEditor(availablePdf)} class="lift flex items-center gap-3 rounded-xl border border-[var(--line)] bg-[var(--paper-strong)] px-4 py-3 text-sm font-semibold"><Crop size={18}/> Edit PDF</button>{/if}<button onclick={()=>sourceDialog=true} class="lift flex items-center gap-3 rounded-xl border border-[var(--line)] bg-[var(--paper-strong)] px-4 py-3 text-sm font-semibold"><Plus size={18}/> Add source</button></div>
  </header>
  {#if sourceMessage}<div class="mb-5 rounded-xl bg-[var(--accent-soft)] px-4 py-3 text-sm">{sourceMessage}</div>{/if}
  {#if session.workflow_kind !== 'subtitles' && workspaceMode === 'automatic'}
    <section class="surface mb-6 flex flex-col gap-4 rounded-3xl border border-[var(--accent)]/25 p-5 sm:flex-row sm:items-center sm:p-6"><div class="grid size-11 shrink-0 place-items-center rounded-2xl bg-[var(--accent-soft)] text-[var(--accent)]"><Sparkles size={21}/></div><div class="min-w-0 flex-1"><h2 class="font-semibold">Generate reviewable audio segments</h2><p class="muted mt-1 text-sm leading-relaxed">Pandrator runs the enabled missing or stale prerequisites in order, then generates segment takes. It stops there: reviewing takes, RVC conversion, assembly, export, and video synchronization remain manual.</p></div><button onclick={generateAutomatically} disabled={!snapshot?.sources.length || snapshot?.stages.find((item)=>item.key==='generate_audio')?.status==='running'} class="flex shrink-0 items-center gap-2 rounded-xl bg-[var(--accent)] px-5 py-3 text-sm font-semibold text-white disabled:opacity-40"><Play size={17}/> Generate audio segments</button></section>
  {:else}
    <div class="mb-6 rounded-2xl border border-[var(--line)] bg-[var(--paper-strong)] px-4 py-3 text-sm"><strong>Review mode:</strong> <span class="muted">run each ready transformation, inspect its artifact, and proceed when satisfied. Downstream cards unlock only when their selected prerequisite exists.</span></div>
  {/if}
  {#if outcome?.pipeline}<div class="mb-6 flex flex-wrap items-center gap-2 rounded-2xl border border-[var(--line)] bg-[var(--paper-strong)] p-4">{#each outcome.pipeline as stage,index}<span class="rounded-lg bg-[var(--accent-soft)] px-3 py-2 text-xs font-semibold">{stage.title}</span>{#if index<outcome.pipeline.length-1}<ChevronRight class="muted" size={14}/>{/if}{/each}</div>{/if}

  {#if error}<div class="mb-5 flex items-start gap-3 rounded-xl border border-red-400/40 bg-red-500/10 px-4 py-3 text-sm"><CircleAlert class="mt-0.5 shrink-0" size={17}/><span>{error}</span></div>{/if}

  {#if loading}
    <div class="surface grid min-h-64 place-items-center rounded-3xl"><LoaderCircle class="animate-spin text-[var(--accent)]" size={28}/></div>
  {:else if snapshot}
    <div class="space-y-4">
      {#each snapshot.stages as stage}
        {@const StatusIcon = statusIcon(stage.status)}
        <article class:stage-locked={stage.status==='unavailable'} class="surface rounded-[1.4rem] p-5 sm:p-6">
          <div class="flex flex-col gap-5 lg:flex-row lg:items-center">
            <div class="flex min-w-0 flex-1 items-start gap-4">
              <div class="grid size-11 shrink-0 place-items-center rounded-2xl bg-[var(--accent-soft)] text-sm font-bold text-[var(--accent)]">{stage.number}</div>
              <div class="min-w-0"><div class="flex flex-wrap items-center gap-2"><h2 class="text-lg font-semibold">{stage.title}</h2><span class:running={stage.status === 'running'} class:done={stage.status === 'completed'} class:warning={stage.status === 'stale' || stage.status === 'failed'} class="status-chip inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[.68rem] font-bold uppercase tracking-wider"><StatusIcon class={stage.status === 'running' ? 'animate-spin' : ''} size={12}/>{stage.toggle?(stage.enabled?'enabled':'disabled'):stage.status}</span></div><p class="muted mt-1.5 max-w-2xl text-sm leading-relaxed">{stage.explanation}</p>{#if stage.status==='running' && stage.progress!=null}<div class="mt-3 h-1.5 max-w-md overflow-hidden rounded-full bg-[var(--line)]"><div class="h-full bg-[var(--accent)]" style={`width:${Math.max(2,stage.progress*100)}%`}></div></div>{/if}{#if stage.detail}<p class="mt-2 text-xs text-red-500">{stage.detail}</p>{/if}{#if stage.key==='optimize_tts' && stage.usage}<p class="muted mt-2 text-xs"><strong class="text-[var(--ink)]">Latest usage:</strong> {stage.usage.total_tokens.toLocaleString()} tokens ({stage.usage.input_tokens.toLocaleString()} input, {stage.usage.output_tokens.toLocaleString()} output{stage.usage.cached_input_tokens ? `, ${stage.usage.cached_input_tokens.toLocaleString()} cached` : ''}) · {formatCost(stage.usage.cost_usd)} · {stage.usage.model_id}</p>{/if}
              {#if (stage.artifacts?.length??0)>0}<StageArtifactHistory artifacts={stage.artifacts??[]} selectedArtifactId={stage.selected_artifact_id} canPreview={Boolean(stage.artifact)} onselect={(artifactId)=>chooseStageArtifact(stage,artifactId)} onpreview={()=>previewArtifact(stage)} onclear={()=>clearStageArtifact(stage)}/>{:else if stage.artifact}<button onclick={() => previewArtifact(stage)} class="mt-2 flex items-center gap-1 text-xs font-semibold text-[var(--accent)]">Preview latest: {stage.artifact.role}<ChevronRight size={13}/></button>{/if}</div>
            </div>
            <div class="flex flex-wrap items-center gap-2 lg:justify-end">
              {#if stage.toggle}
                <button onclick={() => openSettings(stage)} class="flex items-center gap-2 rounded-xl border border-[var(--line)] px-3.5 py-2.5 text-sm font-semibold"><Settings2 size={16}/> Timing &amp; settings</button>
                <label class="flex cursor-pointer items-center gap-3 rounded-xl border border-[var(--line)] px-3.5 py-2.5 text-sm font-semibold"><input type="checkbox" checked={Boolean(stage.enabled)} onchange={(event)=>toggleSpeechOptimization(event.currentTarget.checked)} class="size-4 accent-[var(--accent)]"/> {stage.enabled?'Enabled':'Disabled'}</label>
                {#if !stage.toggle_only && stage.enabled}{#if stage.status==='running'}<button onclick={() => cancel(stage)} class="rounded-xl border border-red-400/50 px-4 py-2.5 text-sm font-semibold text-red-500">Cancel</button>{:else}<button onclick={() => run(stage)} disabled={stage.status === 'unavailable'} class="flex items-center gap-2 rounded-xl bg-[var(--accent)] px-4 py-2.5 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:opacity-35"><Play size={16}/> Run optimization</button>{/if}{/if}
              {:else if stage.executable}
                <button onclick={() => openSettings(stage)} class="flex items-center gap-2 rounded-xl border border-[var(--line)] px-3.5 py-2.5 text-sm font-semibold"><Settings2 size={16}/> Settings</button>
                {#if workspaceMode==='review' || stage.key==='export'}{#if stage.status==='running'}<button onclick={() => cancel(stage)} class="rounded-xl border border-red-400/50 px-4 py-2.5 text-sm font-semibold text-red-500">Cancel</button>{:else}<button onclick={() => run(stage)} disabled={stage.status === 'unavailable'} class="flex items-center gap-2 rounded-xl bg-[var(--accent)] px-4 py-2.5 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:opacity-35"><Play size={16}/> {stage.key==='export'?'Open export':stage.artifact?'Run again':'Run now'}</button>{/if}{/if}
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

{#if sourceDialog}<AddSourceDialog sessionId={session.id} onclose={()=>sourceDialog=false} onadded={sourceAdded}/>{/if}

{#if pendingRun}
  <div class="fixed inset-0 z-[75] grid place-items-center bg-black/40 p-5 backdrop-blur-sm" role="presentation" onclick={(event)=>event.target===event.currentTarget&&(pendingRun=null)}>
    <!-- svelte-ignore a11y_no_noninteractive_element_to_interactive_role -->
    <section class="surface w-full max-w-lg rounded-[1.7rem] p-7" role="dialog" aria-modal="true" aria-labelledby="rerun-title">
      <div class="flex items-start justify-between gap-4"><div><div class="eyebrow">Create another version</div><h2 id="rerun-title" class="mt-1 text-2xl font-semibold">Run {pendingRun.stage.title.toLowerCase()} again?</h2></div><button onclick={()=>pendingRun=null} aria-label="Close rerun confirmation" class="rounded-lg p-2"><X size={19}/></button></div>
      <p class="muted mt-4 text-sm leading-relaxed">A new immutable result will be created and selected only after the run succeeds. The current version and all work based on it remain saved in history.</p>
      {#if pendingRun.impact.dependent_selections?.length}<div class="mt-4 rounded-xl border border-amber-400/40 bg-amber-500/10 p-4 text-sm"><strong>Selections that will need a compatible new version</strong><div class="mt-2 flex flex-wrap gap-2">{#each pendingRun.impact.dependent_selections as dependent}<span class="rounded-full bg-[var(--paper-strong)] px-2.5 py-1 text-xs font-semibold">{artifactRoleLabel(dependent.role)}</span>{/each}</div></div>{/if}
      {#if pendingRun.impact.descendant_total}<p class="muted mt-4 text-xs">{pendingRun.impact.descendant_total} dependent artifact{pendingRun.impact.descendant_total===1?'':'s'}, including audio takes and exports where applicable, will remain available on the earlier path.</p>{/if}
      <div class="mt-6 flex justify-end gap-2"><button onclick={()=>pendingRun=null} class="rounded-xl border border-[var(--line)] px-4 py-2.5 text-sm font-semibold">Cancel</button><button onclick={async()=>{const stage=pendingRun!.stage;pendingRun=null;await run(stage,true)}} class="flex items-center gap-2 rounded-xl bg-[var(--accent)] px-4 py-2.5 text-sm font-semibold text-white"><Play size={16}/> Run and switch when ready</button></div>
    </section>
  </div>
{/if}

{#if pendingSettingsMismatch}
  <div class="fixed inset-0 z-[75] grid place-items-center bg-black/40 p-5 backdrop-blur-sm" role="presentation" onclick={(event)=>event.target===event.currentTarget&&(pendingSettingsMismatch=null)}>
    <!-- svelte-ignore a11y_no_noninteractive_element_to_interactive_role -->
    <section class="surface w-full max-w-lg rounded-[1.7rem] p-7" role="dialog" aria-modal="true" aria-labelledby="mismatch-title">
      <div class="flex items-start justify-between gap-4"><div><div class="eyebrow">Before generation</div><h2 id="mismatch-title" class="mt-1 text-2xl font-semibold">Prerequisite settings changed</h2></div><button onclick={()=>pendingSettingsMismatch=null} aria-label="Close settings change prompt" class="rounded-lg p-2"><X size={19}/></button></div>
      <p class="muted mt-4 text-sm leading-relaxed">These stages already produced output with different settings. Recreating it spends LLM usage; reusing it keeps the current text, takes, and assembled output intact.</p>
      <div class="mt-4 space-y-2">
        {#each pendingSettingsMismatch.mismatches as mismatch}
          <div class="rounded-xl border border-amber-400/40 bg-amber-500/10 p-4 text-sm"><strong>{mismatchStageLabel(mismatch.stage)}</strong><span class="muted"> — changed: {(mismatch.changed_fields?.length ? mismatch.changed_fields : ['settings']).map(mismatchFieldLabel).join(', ')}</span></div>
        {/each}
      </div>
      <div class="mt-6 flex flex-wrap justify-end gap-2">
        <button onclick={()=>pendingSettingsMismatch=null} class="rounded-xl border border-[var(--line)] px-4 py-2.5 text-sm font-semibold">Cancel</button>
        <button onclick={async()=>{const pending=pendingSettingsMismatch!;pendingSettingsMismatch=null;await run(pending.stage,true,pending.mismatches.map((item)=>item.stage))}} class="rounded-xl border border-[var(--line)] px-4 py-2.5 text-sm font-semibold">Use current output</button>
        <button onclick={async()=>{const pending=pendingSettingsMismatch!;pendingSettingsMismatch=null;await run(pending.stage,true)}} class="flex items-center gap-2 rounded-xl bg-[var(--accent)] px-4 py-2.5 text-sm font-semibold text-white"><RefreshCw size={16}/> Rerun</button>
      </div>
    </section>
  </div>
{/if}

{#if settingsStage}
  <div class="fixed inset-0 z-50 grid place-items-center bg-black/35 p-5 backdrop-blur-sm" role="presentation" onclick={(event) => event.target === event.currentTarget && (settingsStage=null)}>
    <div class="surface max-h-[92vh] w-full max-w-xl overflow-y-auto rounded-[1.7rem] p-7" role="dialog" aria-modal="true" aria-labelledby="settings-title">
      <div class="flex justify-between gap-5"><div><div class="eyebrow">Stage settings</div><h2 id="settings-title" class="mt-1 text-2xl font-semibold">{settingsStage.title}</h2></div><button onclick={() => settingsStage=null} class="rounded-lg p-2"><X size={19}/></button></div>
      <div class="mt-6 grid gap-5">
        {#if ['correct','translate','optimize_tts','optimize_document','clean_source'].includes(settingsStage.key)}<label class="text-sm font-semibold">LLM model<select bind:value={model} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"><option value="default">Application default</option>{#each llmModels as item}<option value={item.value}>{item.label}{item.isDefault?' · default':''}</option>{/each}</select></label>{/if}
        {#if settingsStage.key === 'transcribe'}
          <label class="text-sm font-semibold">Recognition model<select bind:value={sttEngine} onchange={()=>sttQuantization=String(capabilities?.stt?.models?.[sttEngine]?.precision??'f16')} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"><option value="whisper">{sttOptionLabel('whisper','Whisper large-v3','DTW timestamps')}</option><option value="parakeet">{sttOptionLabel('parakeet','Parakeet TDT 0.6B v3','native timestamps')}</option><option value="moss">{sttOptionLabel('moss','MOSS Transcribe-Diarize 0.9B','native speakers + CTC words')}</option></select><span class="muted mt-1 block text-xs">CrispASR downloads a model the first time you use it; the installer-selected model is the default.</span></label>
          <label class="text-sm font-semibold">Model precision<select bind:value={sttQuantization} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"><option value="f16">Full F16</option>{#if sttEngine === 'whisper'}<option value="q5_0">Q5_0 · 1.08 GB</option>{:else if sttEngine === 'parakeet'}<option value="q8_0">Q8_0 · 745 MB</option><option value="q5_0">Q5_0 · 541 MB</option><option value="q4_k">Q4_K · 489 MB</option>{:else}<option value="q8_0">Q8_0 · recommended</option><option value="q4_k">Q4_K</option>{/if}</select><span class="muted mt-1 block text-xs">F16 maximizes fidelity; quantized files reduce download and memory use.</span></label>
          <div class="grid gap-3 sm:grid-cols-[1fr_7rem]"><label class="text-sm font-semibold">Compute backend<select bind:value={sttComputeBackend} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"><option value="auto">Automatic</option><option value="cpu" disabled={!supportsSttCompute('cpu')}>CPU</option><option value="cuda" disabled={!supportsSttCompute('cuda')}>CUDA</option><option value="vulkan" disabled={!supportsSttCompute('vulkan')}>Vulkan</option><option value="metal" disabled={!supportsSttCompute('metal')}>Metal</option></select><span class="muted mt-1 block text-xs">Only backends compiled into the installed CrispASR runtime can be forced.</span></label><label class="text-sm font-semibold">Device<input type="number" min="0" disabled={['auto','cpu'].includes(sttComputeBackend)} bind:value={sttDevice} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal disabled:opacity-40"/></label></div>
          {#if sttEngine === 'moss'}
            <div class="rounded-xl border border-[var(--line)] bg-[var(--accent-soft)] p-4"><div class="text-sm font-semibold">Native speaker turns with local CTC timing</div><p class="muted mt-1 text-xs leading-relaxed">MOSS detects the language and speaker changes. Each turn is then aligned separately with Canary CTC and a small acoustic margin, avoiding long-recording alignment drift.</p><div class="mt-3 grid gap-3 sm:grid-cols-2"><label class="flex items-center gap-3 text-xs font-semibold"><input type="checkbox" bind:checked={mossCtcAlignmentEnabled} class="size-4 accent-[var(--accent)]"/> Word-level CTC alignment</label><label class="text-xs font-semibold">CTC padding (s)<input type="number" min="0" max="2" step="0.1" disabled={!mossCtcAlignmentEnabled} bind:value={mossCtcPaddingSeconds} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal disabled:opacity-40"/></label></div></div>
          {:else}
            <div class="grid gap-3 sm:grid-cols-2"><label class="text-sm font-semibold">Source language<select bind:value={originalLanguage} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal">{#each LANGUAGE_OPTIONS as item}<option value={item.value}>{item.label}</option>{/each}</select></label><label class="text-sm font-semibold">Language detector<select bind:value={sttLidBackend} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"><option value="whisper">Whisper tiny</option><option value="ecapa">ECAPA (recommended)</option><option value="silero">Silero</option><option value="off">Off</option></select></label></div>
          {/if}
          {#if sttEngine === 'moss'}<label class="flex items-start gap-3 text-sm font-semibold"><input type="checkbox" bind:checked={mossVadEnabled} class="mt-0.5 size-4 accent-[var(--accent)]"/> <span>Voice activity detection<span class="muted mt-1 block text-xs font-normal">Off by default so native speaker tracking keeps the longest context. The normal chunker still seeks low-energy cut points.</span></span></label>{:else}<label class="flex items-center gap-3 text-sm font-semibold"><input type="checkbox" bind:checked={vadEnabled} class="size-4 accent-[var(--accent)]"/> Voice activity detection</label>{/if}
          {#if (sttEngine === 'moss' ? mossVadEnabled : vadEnabled)}<div class="grid grid-cols-2 gap-3"><label class="text-xs font-semibold">VAD model<select bind:value={vadModel} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"><option value="silero">Silero · general purpose</option><option value="firered">FireRedVAD · robust</option><option value="marblenet">MarbleNet · compact</option><option value="whisper-vad">Whisper VAD · experimental</option></select></label><label class="text-xs font-semibold">VAD threshold<span class="mt-1 grid min-h-10 grid-cols-[1fr_2.5rem] items-center gap-2 rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3"><input type="range" min="0" max="1" step="0.05" bind:value={vadThreshold} class="w-full accent-[var(--accent)]"/><output class="text-right text-xs font-bold">{Number(vadThreshold).toFixed(2)}</output></span></label><label class="text-xs font-semibold">Minimum speech (ms)<input type="number" min="0" bind:value={vadMinSpeech} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"/></label><label class="text-xs font-semibold">Minimum silence (ms)<input type="number" min="0" bind:value={vadMinSilence} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"/></label><label class="text-xs font-semibold">Maximum speech (s)<input type="number" min="1" bind:value={vadMaxSpeech} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"/></label><label class="text-xs font-semibold">Speech padding (ms)<input type="number" min="0" bind:value={vadSpeechPad} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"/></label></div>{/if}
          <details class="rounded-xl border border-[var(--line)] p-4"><summary class="cursor-pointer text-sm font-semibold">Decoder and long-form controls</summary><div class="mt-4 grid grid-cols-2 gap-3"><label class="text-xs font-semibold">Threads (0 = automatic)<input type="number" min="0" bind:value={sttThreads} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"/></label><label class="text-xs font-semibold">Beam size<input type="number" min="1" max="16" bind:value={sttBeamSize} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"/></label>{#if sttEngine === 'parakeet'}<label class="text-xs font-semibold">Parakeet decoder<select bind:value={parakeetDecoder} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"><option value="tdt">TDT greedy / beam</option><option value="maes">MAES beam</option><option value="ctc">CTC greedy</option></select></label>{/if}{#if sttEngine === 'moss'}<label class="text-xs font-semibold">Maximum MOSS context (s)<input type="number" min="30" max="120" step="1" bind:value={mossMaxChunkSeconds} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"/></label>{:else}<label class="text-xs font-semibold">Forced chunk size (s, 0 = default)<input type="number" min="0" step="1" bind:value={sttChunkSeconds} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"/></label>{/if}<label class="text-xs font-semibold">Chunk overlap (s)<input type="number" min="0" step="0.5" bind:value={sttChunkOverlap} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"/></label><label class="col-span-2 text-xs font-semibold">Hotwords<textarea rows="2" bind:value={sttHotwords} placeholder="Names and terminology, comma-separated" class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"></textarea></label></div>{#if sttEngine === 'moss'}<p class="muted mt-3 text-xs">Pandrator uses the longest safe MOSS window, then lets CrispASR seek the lowest-energy point near its limit. Speaker IDs remain local to a chunk; speaker-change boundaries are preserved.</p>{:else}<p class="muted mt-3 text-xs">Parakeet normally preserves full context and handles long recordings internally. Force chunking only for constrained systems or diagnostics.</p>{/if}</details>
          <div class="rounded-xl border border-[var(--line)] p-4"><div class="text-sm font-semibold">Readable subtitle composition</div><p class="muted mt-1 text-xs">Independent from speech blocks and TTS segmentation. Defaults allow 48 characters per line for meetings while retaining two-line, 20 CPS and 0.833–7 second delivery guidance.</p><div class="mt-3 grid grid-cols-2 gap-3"><label class="text-xs font-semibold">Characters / line<input type="number" min="20" max="100" bind:value={subtitleChars} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"/></label><label class="text-xs font-semibold">Lines<input type="number" min="1" max="3" bind:value={subtitleLines} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"/></label><label class="text-xs font-semibold">Minimum duration (ms)<input type="number" min="250" bind:value={subtitleMinDuration} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"/></label><label class="text-xs font-semibold">Maximum duration (ms)<input type="number" min="1000" bind:value={subtitleMaxDuration} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"/></label><label class="text-xs font-semibold">Characters / second<input type="number" min="5" max="40" step="0.5" bind:value={subtitleCps} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"/></label><label class="text-xs font-semibold">Minimum cue gap (ms)<input type="number" min="0" max="500" bind:value={subtitleMinGap} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"/></label><label class="text-xs font-semibold">Phrase-break silence (ms)<input type="number" min="100" max="3000" bind:value={subtitlePhraseGap} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"/></label></div></div>
        {/if}
        {#if settingsStage.key === 'correct'}<label class="text-sm font-semibold">Correction guidance<textarea bind:value={instructions} rows="4" class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"></textarea></label>{/if}
        {#if settingsStage.key === 'translate'}<label class="text-sm font-semibold">Translation backend<select bind:value={backend} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"><option value="llm">LLM</option><option value="deepl">DeepL</option></select></label><label class="text-sm font-semibold">Target language<select bind:value={targetLanguage} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal">{#each LANGUAGE_OPTIONS.filter((item) => item.value !== 'auto') as item}<option value={item.value}>{item.label}</option>{/each}</select></label><label class="text-sm font-semibold">Translation guidance<textarea bind:value={instructions} rows="3" class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"></textarea></label>{/if}
        {#if settingsStage.key==='optimize_tts'}
          <fieldset class="rounded-xl border border-[var(--line)] p-4"><legend class="px-1 text-sm font-semibold">When should optimization run?</legend><div class="mt-2 grid gap-2"><label class="flex items-start gap-3 rounded-xl bg-[var(--accent-soft)] p-3 text-sm"><input type="radio" bind:group={optimizationTiming} value="document" class="mt-1 accent-[var(--accent)]"/><span><strong class="block">Before generation · whole document</strong><span class="muted mt-1 block text-xs">Create an editable before-and-after artifact, review it, then release the LLM before TTS starts.</span></span></label><label class="flex items-start gap-3 rounded-xl bg-[var(--accent-soft)] p-3 text-sm"><input type="radio" bind:group={optimizationTiming} value="generation" class="mt-1 accent-[var(--accent)]"/><span><strong class="block">During generation · segment batches</strong><span class="muted mt-1 block text-xs">Optimize indexed batches as synthesis begins and compare the result in the generation drawer.</span></span></label></div></fieldset>
          <div class="grid gap-3 sm:grid-cols-2"><label class="text-sm font-semibold">Segments per JSON batch{#if optimizationTiming==='document'}<input type="number" min="1" max="64" bind:value={documentOptimizationBatchSize} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"/>{:else}<input type="number" min="1" max="64" bind:value={optimizationBatchSize} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"/>{/if}</label><label class="text-sm font-semibold">Concurrent batches<input type="number" min="1" max="16" bind:value={optimizationConcurrent} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"/></label></div>
          <label class="flex items-start gap-3 rounded-xl border border-[var(--line)] p-4"><input type="checkbox" bind:checked={optimizationMultiStage} class="mt-1 size-4 accent-[var(--accent)]"/><span><span class="block text-sm font-semibold">Use divided prompts</span><span class="muted mt-1 block text-xs">Each non-empty prompt runs sequentially over the same indexed JSON batch.</span></span></label>
          {#if optimizationMultiStage}<label class="text-sm font-semibold">First prompt<textarea bind:value={optimizationFirstPrompt} rows="3" class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"></textarea></label><label class="text-sm font-semibold">Second prompt<textarea bind:value={optimizationSecondPrompt} rows="3" class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"></textarea></label><label class="text-sm font-semibold">Third prompt<textarea bind:value={optimizationThirdPrompt} rows="3" class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"></textarea></label>{:else}<label class="text-sm font-semibold">Single optimization prompt<textarea bind:value={optimizationPrompt} rows="5" placeholder="Leave blank for Pandrator's safe default" class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"></textarea></label>{/if}
        {/if}
        {#if settingsStage.key === 'clean_source'}<label class="flex items-start gap-3 rounded-xl border border-[var(--line)] p-4"><input type="checkbox" bind:checked={agentic} class="mt-1 size-4 accent-[var(--accent)]"/><span><span class="block text-sm font-semibold">Agentic review loop</span><span class="muted mt-1 block text-xs">Runs focused metadata, navigation, boilerplate, repeated-element, and chapter passes. Provider costs may apply.</span></span></label>{#if agentic}<label class="text-sm font-semibold">Maximum LLM turns<input type="number" min="5" max="500" bind:value={maxIterations} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"/></label>{/if}{/if}
        {#if settingsStage.key === 'prepare_text'}<div class="rounded-xl border border-[var(--line)] bg-[var(--accent-soft)] p-4"><div class="text-sm font-semibold">Provider-independent segmentation</div><p class="muted mt-1 text-xs leading-relaxed">These controls create editable narration units and pauses. Voice, model, and synthesis controls are selected later in Generate audio.</p></div><div class="grid gap-3 sm:grid-cols-2"><label class="flex items-center gap-3 rounded-xl border border-[var(--line)] p-3 text-sm font-semibold"><input type="checkbox" bind:checked={splitSentences} class="size-4 accent-[var(--accent)]"/> Split long sentences</label><label class="flex items-center gap-3 rounded-xl border border-[var(--line)] p-3 text-sm font-semibold"><input type="checkbox" bind:checked={appendSentences} class="size-4 accent-[var(--accent)]"/> Join short sentences</label><label class="text-sm font-semibold">Maximum segment length<input type="number" min="20" max="2000" bind:value={maxSentenceLength} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"/></label><label class="flex items-center gap-3 rounded-xl border border-[var(--line)] p-3 text-sm font-semibold"><input type="checkbox" bind:checked={nemoNormalization} class="size-4 accent-[var(--accent)]"/> Deterministic normalization</label></div><details class="rounded-xl border border-[var(--line)] p-4"><summary class="cursor-pointer text-sm font-semibold">Advanced text cleanup</summary><div class="mt-4 grid gap-3 sm:grid-cols-2"><label class="flex items-center gap-3 text-sm"><input type="checkbox" bind:checked={normalizeAllCaps} class="size-4 accent-[var(--accent)]"/> Normalize all-caps text</label><label class="flex items-center gap-3 text-sm"><input type="checkbox" bind:checked={removeDiacritics} class="size-4 accent-[var(--accent)]"/> Remove diacritics</label><label class="flex items-center gap-3 text-sm"><input type="checkbox" bind:checked={removeQuotationMarks} class="size-4 accent-[var(--accent)]"/> Remove quotation marks</label></div></details>{/if}
        {#if settingsStage.key === 'generate_audio'}
          <div class="grid gap-2"><div class="flex items-center justify-between gap-3"><span class="text-sm font-semibold">Active TTS service</span><button type="button" onclick={refreshSpeechServices} disabled={refreshingTtsServices} class="flex items-center gap-2 rounded-lg border border-[var(--line)] px-3 py-2 text-xs font-semibold disabled:opacity-50"><RefreshCw size={14} class={refreshingTtsServices?'animate-spin':''}/> Refresh active backends</button></div><select value={ttsService} onchange={(event)=>chooseTtsService(event.currentTarget.value)} class="w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal">{#each ttsCatalogue.services??[] as service}<option value={service.id}>{service.name}{service.online===true?' · running':service.online===false?' · offline':''}</option>{/each}</select><span class="muted text-xs">Refresh checks every configured backend without stopping another running service.</span></div>
          <div class="flex flex-wrap items-center justify-between gap-3"><p class="muted text-xs">The running configured service is selected automatically. Its live catalogue is refreshed when available.</p><button type="button" onclick={() => ttsServicesOpen = true} class="text-xs font-semibold text-[var(--accent)]">Manage services</button></div>
          <label class="text-sm font-semibold">{selectedTtsServiceId==='kobold_qwen'?'Voice type':'Model'}<select value={ttsModel} onchange={(event)=>chooseTtsModel(event.currentTarget.value)} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal">{#each ttsModels as item}<option value={item}>{item}</option>{/each}</select></label>
          <label class="text-sm font-semibold">Speech language<select bind:value={targetLanguage} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal">{#each (ttsLanguages.length?ttsLanguages:LANGUAGE_OPTIONS.filter((item)=>item.value!=='auto')) as item}<option value={item.value}>{item.label}</option>{/each}</select></label>
          {#if supportsPrebuiltVoices || showClonedVoices}
            <label class="text-sm font-semibold">Voice<select bind:value={voiceName} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal">{#if !showClonedVoices}<option value="">Service default</option>{/if}{#if supportsPrebuiltVoices}<optgroup label={`${LANGUAGE_OPTIONS.find((item)=>item.value===targetLanguage)?.label??targetLanguage} · pre-built voices`}>{#each filteredPrebuiltVoices as voice}<option value={voice.id}>{voice.name}{voice.gender?` · ${voice.gender}`:''}</option>{/each}</optgroup>{/if}{#if showClonedVoices}<optgroup label="Voices ready in provider">{#each clonedVoiceDescriptors as voice}<option value={voice.id}>{voice.name}</option>{/each}</optgroup>{/if}</select></label>
            <div class="flex flex-wrap items-center justify-between gap-3"><p class="muted text-xs">{showClonedVoices?'Only voices returned by this provider or uploaded from the Library can be selected.':'Only voices supported by the selected model are shown.'}</p>{#if showClonedVoices}<button type="button" onclick={()=>openVoiceLibrary('references',selectedTtsServiceId)} class="flex items-center gap-1.5 text-xs font-semibold text-[var(--accent)]"><Library size={14}/> Create or upload cloned voice</button>{:else}<button type="button" onclick={()=>openVoiceLibrary('prebuilt')} class="flex items-center gap-1.5 text-xs font-semibold text-[var(--accent)]"><Library size={14}/> Browse pre-built voices</button>{/if}</div>
          {/if}
          {#if supportsGenerationPrompt}
            <label class="text-sm font-semibold">Speech direction<textarea bind:value={generationPrompt} rows="4" placeholder="For example: Warm, intimate narration with measured pacing and subtle excitement." class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"></textarea><span class="muted mt-2 block text-xs">Sent with every segment as performance guidance. It does not rewrite the transcript and should not be spoken aloud.</span></label>
          {:else if generationPromptModels.length}
            <p class="muted rounded-xl bg-[var(--accent-soft)] p-3 text-xs">{ttsModel || 'This model'} does not accept speech-direction prompts. Choose an instruction-capable model to add one.</p>
          {/if}
          {#if session.workflow_kind !== 'audiobook'}<div class="rounded-xl border border-[var(--line)] p-4"><div class="text-sm font-semibold">Speech blocks for dubbing</div><p class="muted mt-1 text-xs">These TTS chunks are independent from the final subtitle layout.</p><div class="mt-3 grid grid-cols-2 gap-3"><label class="text-xs font-semibold">Minimum characters<input type="number" min="1" bind:value={speechBlockMinChars} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"/></label><label class="text-xs font-semibold">Maximum characters<input type="number" min="1" bind:value={speechBlockMaxChars} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"/></label><label class="text-xs font-semibold">Merge gap (ms)<input type="number" min="0" bind:value={speechBlockMergeThreshold} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"/></label></div></div>{/if}
        {/if}
        {#if settingsStage.key === 'export'}
          <label class="text-sm font-semibold">Export target<select bind:value={exportMode} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal">{#if session.workflow_kind!=='subtitles'}<option value="media">Rendered video / media</option>{/if}<option value="subtitles">Subtitle file</option><option value="text">Concatenated plain text</option></select></label>
          {#if exportMode==='media'}
            <label class="text-sm font-semibold">Audio<select bind:value={audioMode} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"><option value="mixed">Mix source and dubbing (recommended)</option><option value="preserve">Preserve source audio</option><option value="dubbing_only">Dubbing only</option></select></label><label class="text-sm font-semibold">Subtitles<select bind:value={subtitleMode} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"><option value="none">None</option><option value="soft">Injected soft tracks</option><option value="burned">Burned subtitles</option></select></label>
          {:else if exportMode==='subtitles'}
            <label class="text-sm font-semibold">Subtitle format<select bind:value={subtitleFormat} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"><option value="srt">SubRip (.srt)</option><option value="vtt">WebVTT (.vtt)</option></select></label>
          {:else}<p class="muted rounded-xl bg-[var(--accent-soft)] p-3 text-xs">Cue timestamps and numbering are removed and the selected subtitle text is joined into one plain-text document.</p>{/if}
          {#if exportMode!=='media'||subtitleMode!=='none'}<label class="text-sm font-semibold">Subtitle tracks<select bind:value={subtitleSelection} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"><option value="source">Source / corrected</option><option value="translation">Translation</option><option value="dual">Source and translation</option></select></label>{/if}
          <div class="rounded-xl border border-[var(--line)] p-4"><div class="text-sm font-semibold">Final subtitle layout</div><p class="muted mt-1 text-xs">Applied only to derived export subtitles; source and reviewed revisions remain unchanged.</p><div class="mt-3 grid grid-cols-2 gap-3"><label class="text-xs font-semibold">Characters / line<input type="number" min="20" max="100" bind:value={subtitleChars} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"/></label><label class="text-xs font-semibold">Lines<input type="number" min="1" max="3" bind:value={subtitleLines} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"/></label><label class="text-xs font-semibold">Minimum duration (ms)<input type="number" min="250" bind:value={subtitleMinDuration} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"/></label><label class="text-xs font-semibold">Maximum duration (ms)<input type="number" min="1000" bind:value={subtitleMaxDuration} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"/></label><label class="text-xs font-semibold">Characters / second<input type="number" min="5" max="40" step="0.5" bind:value={subtitleCps} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"/></label><label class="text-xs font-semibold">Minimum cue gap (ms)<input type="number" min="0" max="500" bind:value={subtitleMinGap} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"/></label><label class="text-xs font-semibold">Phrase-break silence (ms)<input type="number" min="100" max="3000" bind:value={subtitlePhraseGap} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"/></label></div></div>
        {/if}
      </div>
      {#if stageMessage}<p role="status" class="mt-5 rounded-xl bg-[var(--accent-soft)] p-3 text-xs">{stageMessage}</p>{/if}
      <div class="mt-7 flex flex-wrap justify-end gap-3"><button onclick={() => { fullSettingsSection=stageSection(settingsStage!.key); settingsStage=null; }} class="mr-auto rounded-xl border border-[var(--line)] px-4 py-2.5 text-sm font-semibold">All {sectionDisplay(stageSection(settingsStage.key))} settings</button><button onclick={revertStageToDefaults} class="flex items-center gap-2 rounded-xl border border-[var(--line)] px-4 py-2.5 text-sm font-semibold"><RotateCcw size={15}/> Revert to defaults</button><button onclick={() => saveSettings('defaults')} class="flex items-center gap-2 rounded-xl border border-[var(--line)] px-4 py-2.5 text-sm font-semibold"><Save size={15}/> Save as defaults</button><button onclick={() => settingsStage=null} class="rounded-xl border border-[var(--line)] px-4 py-2.5 text-sm font-semibold">Cancel</button><button onclick={() => saveSettings('session')} class="rounded-xl bg-[var(--accent)] px-4 py-2.5 text-sm font-semibold text-white">Save settings</button></div>
    </div>
  </div>
{/if}

{#if pdfSource && PdfEditorComponent}<PdfEditorComponent sessionId={session.id} source={pdfSource} onclose={() => pdfSource=null}/>{/if}
{#if reviewOpen}<SubtitleReview sessionId={session.id} sourceArtifactId={snapshot?.sources[0]?.id} onclose={() => reviewOpen=false} onsaved={load}/>{/if}
{#if preview}<ArtifactPreview artifact={preview} onclose={()=>preview=null}/>{/if}
{#if optimizationReviewArtifactId}<TextOptimizationReview artifactId={optimizationReviewArtifactId} onclose={()=>optimizationReviewArtifactId=''} onsaved={load}/>{/if}
{#if fullSettingsSection}<SettingsModal sessionId={session.id} section={fullSettingsSection} title={`${sectionDisplay(fullSettingsSection)} settings`} description="These settings are saved as session overrides and inherited by future runs." onclose={()=>fullSettingsSection=''}/>{/if}
{#if ttsServicesOpen}<TtsServicesModal onclose={async() => { ttsServicesOpen=false; await loadSpeechCatalogues(true); }}/>{/if}
{#if voiceLibraryOpen}<VoiceLibraryModal initialView={voiceLibraryView} initialService={voiceLibraryService} onvoicepublished={usePublishedVoice} onclose={async()=>{voiceLibraryOpen=false;await loadSpeechCatalogues(true)}}/>{/if}
<GuidedTour tourId="workflow" steps={workflowTourSteps} bind:open={workflowTour}/>

<style>
  .status-chip { color: var(--muted); background: color-mix(in srgb, var(--muted) 10%, transparent); }
  .status-chip.done { color: var(--success); background: color-mix(in srgb, var(--success) 12%, transparent); }
  .status-chip.running { color: var(--accent); background: var(--accent-soft); }
  .status-chip.warning { color: var(--warning); background: color-mix(in srgb, var(--warning) 12%, transparent); }
  .mode-choice { border-radius: .65rem; padding: .55rem .85rem; font-size: .75rem; font-weight: 700; color: var(--muted); }
  .mode-choice.mode-active { background: var(--action-bg); color: white; box-shadow: 0 4px 14px color-mix(in srgb, var(--accent) 24%, transparent); }
  .stage-locked { opacity: .58; }
</style>
