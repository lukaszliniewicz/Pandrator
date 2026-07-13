<script lang="ts">
  import { Check, CircleAlert, Library, Play, Plus, RefreshCw, Save, Trash2, Volume2 } from '@lucide/svelte';
  import { api, type JobRecord } from './api';
  import { describeVoice, languagesForService, type VoiceDescriptor } from './voice-catalog';
  import AudioPlayer from './AudioPlayer.svelte';

  let { initialService = '' }: { initialService?: string } = $props();
  let payload = $state<any>({ services: [], value: {}, revision: 0 });
  let serviceId = $state('');
  let model = $state('');
  let language = $state('');
  let previewText = $state('The quick brown fox jumps over the lazy dog.');
  let previews = $state<Record<string, { status: string; artifactId?: string; error?: string }>>({});
  let error = $state('');
  let notice = $state('');
  let generatingAll = $state(false);
  let refreshing = $state(false);
  let generatedCount = $state(0);
  let newVoice = $state('');

  const services = $derived((payload.services ?? []).filter((service: any) => service.supports_prebuilt_voices));
  const service = $derived(services.find((item: any) => item.id === serviceId) ?? services[0]);
  const models = $derived(Array.from(new Set([...(service?.models ?? []), service?.default_model].filter(Boolean))));
  const rawVoices = $derived(Array.from(new Set([...(service?.voice_catalogues?.[model] ?? service?.voices ?? []), service?.default_voices?.[model], service?.default_voice].filter(Boolean))) as string[]);
  const descriptors = $derived(rawVoices.map((voice) => describeVoice(service?.id ?? '', voice)));
  const languages = $derived(languagesForService(service?.id ?? '', descriptors));
  const visibleVoices = $derived(descriptors.filter((voice) => !language || !voice.languageCode || voice.languageCode === language));
  const languageDefault = $derived(String(service?.default_voices_by_language?.[model]?.[language] ?? service?.default_voices?.[model] ?? service?.default_voice ?? ''));

  $effect(() => {
    if (service && serviceId !== service.id) serviceId = service.id;
    if (service && (!model || !models.includes(model))) model = service.default_model || models[0] || '';
  });
  $effect(() => {
    if (languages.length && !languages.some((item) => item.value === language)) language = languages[0].value;
    if (!languages.length) language = '';
  });

  function configuredRecord(candidate: any) {
    return (payload.value.provider_configs ?? []).find((item: any) => item.id === candidate.id || item.api_base === candidate.api_base);
  }

  function editableRecord(candidate: any) {
    const existing = configuredRecord(candidate) ?? {};
    return {
      ...existing,
      id: existing.id ?? candidate.id,
      name: existing.name ?? candidate.name,
      kind: existing.kind ?? candidate.kind,
      api_base: existing.api_base ?? candidate.api_base,
      provider: existing.provider ?? candidate.provider,
      adapter: existing.adapter ?? candidate.adapter,
      models: candidate.models ?? existing.models ?? [],
      voices: candidate.voices ?? existing.voices ?? [],
      default_model: candidate.default_model ?? existing.default_model,
      default_voice: candidate.default_voice ?? existing.default_voice,
      voice_catalogues: candidate.voice_catalogues ?? existing.voice_catalogues ?? {},
      default_voices: candidate.default_voices ?? existing.default_voices ?? {},
      default_voices_by_language: candidate.default_voices_by_language ?? existing.default_voices_by_language ?? {},
      settings: existing.settings ?? candidate.settings ?? {},
      supports_prebuilt_voices: candidate.supports_prebuilt_voices
    };
  }

  async function load() {
    payload = await api('/services/tts');
    if (initialService && services.some((item: any) => item.id === initialService)) serviceId = initialService;
  }

  async function saveRecord(record: any) {
    const value = { ...payload.value, provider_configs: [...(payload.value.provider_configs ?? []).filter((item: any) => item.id !== record.id && item.api_base !== record.api_base), record] };
    await api('/settings/services.tts', { method: 'PUT', headers: { 'If-Match': `"${payload.revision}"` }, body: JSON.stringify({ value }) });
    await load();
  }

  async function refreshCatalogue() {
    if (!service || refreshing) return;
    refreshing = true; error = ''; notice = '';
    try {
      const found = await api<any>('/services/tts/discover', { method: 'POST', body: JSON.stringify({ base_url: service.api_base }) });
      const record = editableRecord(service);
      const discoveredModels = (found.models ?? []).filter(Boolean);
      const discoveredVoices = (found.voices ?? []).filter(Boolean);
      record.models = Array.from(new Set([...(record.models ?? []), ...discoveredModels]));
      record.voices = Array.from(new Set([...(record.voices ?? []), ...discoveredVoices]));
      record.default_model ||= found.default_model || record.models[0] || '';
      record.default_voice ||= found.default_voice || '';
      for (const key of ['adapter', 'speech_path', 'models_path', 'voices_path', 'request_fields', 'request_defaults']) {
        if (found[key] != null && found[key] !== '') record[key] = found[key];
      }
      if (discoveredVoices.length) {
        const catalogueModels = discoveredModels.length ? discoveredModels : [model || record.default_model].filter(Boolean);
        const catalogues = { ...(record.voice_catalogues ?? {}) };
        for (const catalogueModel of catalogueModels) {
          catalogues[catalogueModel] = Array.from(new Set([...(catalogues[catalogueModel] ?? []), ...discoveredVoices]));
        }
        record.voice_catalogues = catalogues;
      }
      await saveRecord(record);
      notice = `Catalogue refreshed: ${discoveredModels.length} model${discoveredModels.length === 1 ? '' : 's'} and ${discoveredVoices.length} voice${discoveredVoices.length === 1 ? '' : 's'} discovered. Manual entries and defaults were preserved.`;
    } catch (caught) {
      error = caught instanceof Error ? caught.message : String(caught);
    } finally {
      refreshing = false;
    }
  }

  async function saveDefault(voice: VoiceDescriptor) {
    error = ''; notice = '';
    try {
      const record = editableRecord(service);
      const byLanguage = { ...(record.default_voices_by_language ?? {}) };
      byLanguage[model] = { ...(byLanguage[model] ?? {}), ...(language ? { [language]: voice.id } : {}) };
      record.default_voices_by_language = byLanguage;
      if (!language) record.default_voices = { ...(record.default_voices ?? {}), [model]: voice.id };
      if (!language && model === record.default_model) record.default_voice = voice.id;
      await saveRecord(record);
      notice = `${voice.name} is now the default${language ? ` for ${languages.find((item) => item.value === language)?.label}` : ''}.`;
    } catch (caught) { error = caught instanceof Error ? caught.message : String(caught); }
  }

  async function addCatalogueVoice() {
    const id = newVoice.trim();
    if (!id || !service || !model) return;
    try {
      const record = editableRecord(service);
      const catalogue = Array.from(new Set([...(record.voice_catalogues?.[model] ?? record.voices ?? []), id]));
      record.voice_catalogues = { ...(record.voice_catalogues ?? {}), [model]: catalogue };
      record.voices = Array.from(new Set([...(record.voices ?? []), id]));
      await saveRecord(record);
      newVoice = '';
      notice = 'Voice catalogue entry added.';
    } catch (caught) { error = caught instanceof Error ? caught.message : String(caught); }
  }

  async function removeCatalogueVoice(voice: VoiceDescriptor) {
    try {
      const record = editableRecord(service);
      record.voice_catalogues = { ...(record.voice_catalogues ?? {}), [model]: (record.voice_catalogues?.[model] ?? record.voices ?? []).filter((item: string) => item !== voice.id) };
      record.voices = (record.voices ?? []).filter((item: string) => item !== voice.id);
      await saveRecord(record);
      notice = `${voice.name} removed from the saved catalogue.`;
    } catch (caught) { error = caught instanceof Error ? caught.message : String(caught); }
  }

  async function waitJob(id: string) {
    for (let attempt = 0; attempt < 240; attempt += 1) {
      const job = await api<JobRecord>(`/jobs/${id}`);
      if (job.status === 'succeeded') return job;
      if (['failed', 'canceled', 'interrupted'].includes(job.status)) throw new Error(job.error_message || `Preview ${job.status}.`);
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
    throw new Error('Preview is still running. Check Activity & logs.');
  }

  async function preview(voice: VoiceDescriptor, quiet = false) {
    if (!previewText.trim()) { error = 'Enter preview text first.'; return false; }
    previews = { ...previews, [voice.id]: { status: 'generating' } };
    if (!quiet) { error = ''; notice = ''; }
    try {
      const queued = await api<JobRecord>(`/services/tts/${service.id}/preview`, { method: 'POST', body: JSON.stringify({ text: previewText.trim(), model, voice: voice.id, language }) });
      const complete = await waitJob(queued.id);
      previews = { ...previews, [voice.id]: { status: 'ready', artifactId: String(complete.result_json?.artifact_id ?? '') } };
      return true;
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : String(caught);
      previews = { ...previews, [voice.id]: { status: 'failed', error: message } };
      if (!quiet) error = message;
      return false;
    }
  }

  async function generateVisible() {
    if (!visibleVoices.length || generatingAll) return;
    generatingAll = true; generatedCount = 0; error = ''; notice = '';
    let failures = 0;
    for (const voice of visibleVoices) {
      if (!(await preview(voice, true))) failures += 1;
      generatedCount += 1;
    }
    generatingAll = false;
    notice = `Generated ${visibleVoices.length - failures} of ${visibleVoices.length} previews${failures ? `; ${failures} failed` : ''}.`;
  }

  load().catch((caught) => error = caught instanceof Error ? caught.message : String(caught));
</script>

<section>
  <div class="flex flex-wrap items-end justify-between gap-4">
    <div><div class="eyebrow">Pre-built catalogue</div><h2 class="mt-1 text-2xl font-semibold">Browse voices</h2><p class="muted mt-2 max-w-2xl text-sm">Compare provider voices with the same text, grouped by language, without leaving the Voice Library.</p></div>
    <button onclick={refreshCatalogue} disabled={!service || refreshing} class="btn btn-secondary"><RefreshCw class={refreshing ? 'animate-spin' : ''} size={15}/> {refreshing ? 'Refreshing…' : 'Refresh service catalogue'}</button>
  </div>
  {#if error}<div role="alert" class="mt-4 flex items-start gap-2 rounded-xl border border-red-400/40 bg-red-500/10 px-4 py-3 text-sm"><CircleAlert class="mt-0.5 shrink-0" size={16}/><span>{error}</span></div>{/if}
  {#if notice}<div role="status" class="mt-4 rounded-xl bg-[var(--accent-soft)] px-4 py-3 text-sm">{notice}</div>{/if}

  <div class="surface mt-5 rounded-3xl p-4 sm:p-6">
    <div class="catalogue-controls">
      <label class="control-label">Service<select bind:value={serviceId} class="field"><option value="">Choose a service</option>{#each services as item}<option value={item.id}>{item.name}</option>{/each}</select></label>
      <label class="control-label">Model<select bind:value={model} class="field">{#each models as item}<option value={item}>{item}</option>{/each}</select></label>
      <label class="control-label">Language<select bind:value={language} class="field" disabled={!languages.length}>{#if !languages.length}<option value="">Multilingual</option>{/if}{#each languages as item}<option value={item.value}>{item.label}</option>{/each}</select></label>
    </div>
    <label class="mt-5 block text-sm font-semibold">Preview text<textarea bind:value={previewText} rows="2" maxlength="1000" class="field resize-y"></textarea></label>
    <div class="mt-4 flex flex-wrap items-center justify-between gap-3"><p class="muted text-xs">{visibleVoices.length} voice{visibleVoices.length === 1 ? '' : 's'} in this view. Preview generation runs sequentially so the speech service is not overloaded.</p><button onclick={generateVisible} disabled={!visibleVoices.length || generatingAll} class="btn btn-primary"><Volume2 size={15}/>{generatingAll ? `Generating ${generatedCount + 1} of ${visibleVoices.length}…` : `Generate all ${languages.length ? 'for this language' : 'visible voices'}`}</button></div>
  </div>

  <div class="mt-5 grid gap-3">
    {#each visibleVoices as voice}
      {@const state = previews[voice.id]}
      <article class="voice-row rounded-2xl border border-[var(--line)] bg-[var(--paper-strong)] p-4">
        <div class="min-w-0"><div class="flex flex-wrap items-center gap-2"><h3 class="font-semibold">{voice.name}</h3>{#if voice.id === languageDefault}<span class="inline-flex items-center gap-1 rounded-full bg-[var(--accent-soft)] px-2 py-0.5 text-[.65rem] font-bold uppercase text-[var(--accent)]"><Check size={10}/> Default</span>{/if}</div><p class="muted mt-1 break-all text-xs">{voice.id}</p></div>
        <div class="voice-meta"><span>{voice.language}</span>{#if voice.gender}<span>{voice.gender}</span>{/if}</div>
        <div class="flex flex-wrap justify-end gap-2"><button onclick={() => preview(voice)} disabled={state?.status === 'generating' || generatingAll} class="btn btn-sm btn-secondary"><Play size={13}/>{state?.status === 'generating' ? 'Generating…' : state?.status === 'ready' ? 'Regenerate' : 'Preview'}</button><button onclick={() => saveDefault(voice)} disabled={voice.id === languageDefault} class="btn btn-sm btn-secondary"><Save size={13}/> Use by default</button></div>
        {#if state?.artifactId}<div class="col-span-full"><AudioPlayer src={`/api/v1/artifacts/${state.artifactId}/content`} label={`${voice.name} preview`}/></div>{/if}
        {#if state?.error}<p class="col-span-full text-xs text-red-600">{state.error}</p>{/if}
      </article>
    {:else}
      <div class="muted rounded-2xl border border-dashed border-[var(--line)] p-10 text-center"><Library class="mx-auto mb-2" size={22}/> This model does not expose pre-built voices.</div>
    {/each}
  </div>

  {#if service}
    <details class="mt-5 rounded-2xl border border-[var(--line)] p-4"><summary class="cursor-pointer text-sm font-semibold">Manage custom catalogue entries</summary><p class="muted mt-2 text-xs">Add an ID exposed by this endpoint. Built-in entries reappear when the service catalogue is refreshed.</p><div class="mt-3 flex gap-2"><input bind:value={newVoice} onkeydown={(event) => { if (event.key === 'Enter') { event.preventDefault(); addCatalogueVoice(); } }} placeholder="Voice ID" class="field mt-0 min-w-0 flex-1"/><button onclick={addCatalogueVoice} class="btn btn-secondary"><Plus size={14}/> Add</button></div>{#if rawVoices.length}<div class="mt-3 flex flex-wrap gap-2">{#each rawVoices as voice}<span class="inline-flex items-center gap-1 rounded-full bg-[var(--accent-soft)] px-2.5 py-1 text-xs">{describeVoice(service.id, voice).name}<button onclick={() => removeCatalogueVoice(describeVoice(service.id, voice))} aria-label={`Remove ${voice}`}><Trash2 size={11}/></button></span>{/each}</div>{/if}</details>
  {/if}
</section>

<style>
  .field{margin-top:.4rem;width:100%;min-width:0;border:1px solid var(--line);border-radius:.75rem;background:var(--paper);padding:.68rem .78rem;font-weight:400;color:var(--ink)}
  .control-label{min-width:0;font-size:.78rem;font-weight:700}.catalogue-controls{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:.8rem}
  .voice-row{display:grid;grid-template-columns:minmax(12rem,1fr) minmax(9rem,.45fr) auto;align-items:center;gap:1rem}.voice-meta{display:flex;flex-wrap:wrap;gap:.4rem;color:var(--muted);font-size:.75rem}.voice-meta span{border-radius:999px;background:var(--accent-soft);padding:.25rem .55rem}
  @media(max-width:800px){.voice-row{grid-template-columns:minmax(0,1fr) auto}.voice-meta{grid-column:1}.voice-row>div:nth-of-type(3){grid-column:2;grid-row:1/3}.catalogue-controls{grid-template-columns:1fr 1fr}.catalogue-controls label:last-child{grid-column:1/-1}}
  @media(max-width:560px){.catalogue-controls,.voice-row{grid-template-columns:minmax(0,1fr)}.catalogue-controls label:last-child,.voice-meta,.voice-row>div:nth-of-type(3){grid-column:1;grid-row:auto}.voice-row>div:nth-of-type(3){justify-content:flex-start}}
</style>
