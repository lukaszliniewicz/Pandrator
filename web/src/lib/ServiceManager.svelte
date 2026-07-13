<script lang="ts">
  import { CheckCircle2, ExternalLink, Play, Plus, RefreshCw, Server, Settings2, Trash2, X } from '@lucide/svelte';
  import { api } from './api';
  import SettingField from './SettingField.svelte';

  const SERVICE_SETTING_KEYS: Record<string, string[]> = {
    xtts: ['temperature', 'length_penalty', 'repetition_penalty', 'top_k', 'top_p', 'do_sample', 'num_beams', 'enable_text_splitting', 'stream_chunk_size', 'gpt_cond_len', 'gpt_cond_chunk_len', 'max_ref_len', 'sound_norm_refs', 'overlap_wav_len'],
    voxcpm: ['voxcpm_cfg_value', 'voxcpm_inference_timesteps', 'voxcpm_normalize', 'voxcpm_denoise', 'voxcpm_retry_badcase', 'voxcpm_retry_badcase_max_times', 'voxcpm_retry_badcase_ratio_threshold', 'voxcpm_min_len', 'voxcpm_max_len'],
    fishs2: ['fishs2_temperature', 'fishs2_top_p', 'fishs2_chunk_length', 'fishs2_latency', 'fishs2_normalize', 'fishs2_prosody_volume', 'fishs2_normalize_loudness'],
    voxtral: ['voxtral_max_frames', 'voxtral_euler_steps', 'voxtral_chunk', 'voxtral_max_chunk_chars', 'voxtral_chunk_silence_ms', 'voxtral_strip_quotes', 'voxtral_strip_diacritics', 'voxtral_level_audio'],
    chatterbox: ['chatterbox_temperature', 'chatterbox_repetition_penalty', 'chatterbox_min_p', 'chatterbox_top_p', 'chatterbox_top_k', 'chatterbox_exaggeration', 'chatterbox_cfg_weight', 'chatterbox_norm_loudness'],
    openai: ['openai_audio_instructions'], gemini: ['openai_audio_instructions']
  };

  let payload = $state<any>({ services: [], profiles: [], value: {}, revision: 0, default_value: {}, default_service: 'XTTS', default_revision: 0, builtin_defaults: {} });
  let discoverOpen = $state(false);
  let baseUrl = $state('http://127.0.0.1:8000');
  let discovering = $state(false);
  let refreshing = $state('');
  let result = $state<any>(null);
  let error = $state('');
  let editing = $state<any>(null);
  let selectedModel = $state('');
  let newVoice = $state('');
  let previewText = $state('Welcome to Pandrator. This is a preview of the selected voice.');
  let previewing = $state(false);
  let previewArtifactId = $state('');

  const normalizedId = (service: any) => String(service?.id ?? '').toLowerCase().replaceAll('-', '_');
  const settingKeys = $derived(SERVICE_SETTING_KEYS[normalizedId(editing)] ?? []);
  const modelChoices = $derived(Array.from(new Set([...(editing?.models ?? []), editing?.default_model].filter(Boolean))));
  const voiceCatalogue = $derived(editing ? Array.from(new Set([...(editing.voice_catalogues?.[selectedModel] ?? editing.voices ?? []), editing.default_voices?.[selectedModel], editing.default_voice].filter(Boolean))) : []);
  const activeDefaultVoice = $derived(editing ? String(editing.default_voices?.[selectedModel] ?? (selectedModel === editing.default_model ? editing.default_voice : '') ?? '') : '');

  async function load() { payload = await api('/services/tts'); }

  async function persist(value: any) {
    await api('/settings/services.tts', { method: 'PUT', headers: { 'If-Match': `"${payload.revision}"` }, body: JSON.stringify({ value }) });
    await load();
  }

  function recordFrom(candidate: any, existing: any = {}) {
    return { ...existing, id: existing.id ?? candidate.id ?? String(candidate.name ?? 'service').toLowerCase().replace(/[^a-z0-9]+/g, '_'), name: candidate.name, kind: candidate.kind, api_base: candidate.api_base, provider: candidate.provider, adapter: candidate.adapter, speech_path: candidate.speech_path, models_path: candidate.models_path, voices_path: candidate.voices_path, request_fields: candidate.request_fields, request_defaults: candidate.request_defaults, models: candidate.models ?? [], voices: candidate.voices ?? [], default_model: candidate.default_model, default_voice: candidate.default_voice, voice_catalogues: candidate.voice_catalogues ?? existing.voice_catalogues ?? {}, default_voices: candidate.default_voices ?? existing.default_voices ?? {}, settings: candidate.settings ?? existing.settings ?? {}, supports_prebuilt_voices: candidate.supports_prebuilt_voices };
  }

  function configuredRecord(service: any) {
    return (payload.value.provider_configs ?? []).find((item: any) => item.id === service.id || item.api_base === service.api_base);
  }

  function openSettings(service: any) {
    const saved = configuredRecord(service) ?? {};
    editing = recordFrom({ ...service, ...saved }, saved);
    selectedModel = editing.default_model || editing.models?.[0] || '';
    if (selectedModel && !editing.models?.includes(selectedModel)) editing.models = [selectedModel, ...(editing.models ?? [])];
    editing = { ...editing, settings: { ...(editing.settings ?? {}) }, voice_catalogues: { ...(editing.voice_catalogues ?? {}) }, default_voices: { ...(editing.default_voices ?? {}) } };
    newVoice = '';
    previewArtifactId = '';
    error = '';
  }

  function setServiceSetting(key: string, value: any) { editing = { ...editing, settings: { ...(editing.settings ?? {}), [key]: value } }; }
  function setDefaultVoice(value: string) { editing = { ...editing, default_voice: selectedModel === editing.default_model ? value : editing.default_voice, default_voices: { ...(editing.default_voices ?? {}), [selectedModel]: value } }; }
  function setDefaultModel(value: string) { selectedModel = value; editing = { ...editing, default_model: value, models: Array.from(new Set([...(editing.models ?? []), value].filter(Boolean))) }; }

  function addVoice() {
    const voice = newVoice.trim();
    if (!voice || !selectedModel) return;
    const next = Array.from(new Set([...voiceCatalogue, voice]));
    editing = { ...editing, voices: Array.from(new Set([...(editing.voices ?? []), voice])), voice_catalogues: { ...(editing.voice_catalogues ?? {}), [selectedModel]: next } };
    if (!activeDefaultVoice) setDefaultVoice(voice);
    newVoice = '';
  }

  function removeVoice(voice: string) {
    const next = voiceCatalogue.filter((item) => item !== voice);
    const defaults = { ...(editing.default_voices ?? {}) };
    if (defaults[selectedModel] === voice) defaults[selectedModel] = next[0] ?? '';
    editing = { ...editing, voices: (editing.voices ?? []).filter((item: string) => item !== voice || Object.entries(editing.voice_catalogues ?? {}).some(([model, items]: any) => model !== selectedModel && Array.isArray(items) && items.includes(voice))), voice_catalogues: { ...(editing.voice_catalogues ?? {}), [selectedModel]: next }, default_voices: defaults, default_voice: selectedModel === editing.default_model ? (defaults[selectedModel] ?? '') : editing.default_voice };
  }

  function buildEditingRecord() {
    const catalogues = editing.voice_catalogues ?? {};
    const voices = Array.from(new Set(Object.values(catalogues).flatMap((items: any) => Array.isArray(items) ? items : []).concat(editing.voices ?? [])));
    const defaultModel = editing.default_model || selectedModel;
    const defaultVoice = editing.default_voices?.[defaultModel] ?? editing.default_voice ?? '';
    return recordFrom({ ...editing, models: modelChoices, voices, default_model: defaultModel, default_voice: defaultVoice }, editing);
  }

  async function persistEditing(close = true) {
    if (!editing) return;
    const record = buildEditingRecord();
    await persist({ ...payload.value, provider_configs: [...(payload.value.provider_configs ?? []).filter((item: any) => item.id !== record.id && item.api_base !== record.api_base), record] });
    if (close) editing = null;
  }

  async function previewVoice() {
    if (!previewText.trim()) { error = 'Enter preview text first.'; return; }
    previewing = true; previewArtifactId = ''; error = '';
    try {
      const serviceId = editing.id;
      await persistEditing(false);
      const job = await api<any>(`/services/tts/${serviceId}/preview`, { method: 'POST', body: JSON.stringify({ text: previewText.trim(), model: selectedModel, voice: activeDefaultVoice }) });
      for (let attempt = 0; attempt < 120; attempt += 1) {
        const status = await api<any>(`/jobs/${job.id}`);
        if (status.status === 'succeeded') { previewArtifactId = status.result_json?.artifact_id ?? ''; break; }
        if (['failed', 'canceled', 'interrupted'].includes(status.status)) throw new Error(status.error_message || 'Voice preview failed.');
        await new Promise((resolve) => setTimeout(resolve, 500));
      }
      if (!previewArtifactId) throw new Error('Voice preview is still pending. Check Activity & logs.');
    } catch (caught) { error = caught instanceof Error ? caught.message : String(caught); }
    finally { previewing = false; }
  }

  async function discover() { discovering = true; error = ''; try { result = await api('/services/tts/discover', { method: 'POST', body: JSON.stringify({ base_url: baseUrl }) }); } catch (caught) { error = caught instanceof Error ? caught.message : String(caught); } finally { discovering = false; } }
  async function saveDiscovered() { if (!result?.success) return; const existing = (payload.value.provider_configs ?? []).find((item: any) => item.api_base === result.api_base); await persist({ ...payload.value, provider_configs: [...(payload.value.provider_configs ?? []).filter((item: any) => item.api_base !== result.api_base), recordFrom(result, existing)] }); discoverOpen = false; result = null; }
  async function useProfile(profile: any) { const existing = (payload.value.provider_configs ?? []).find((item: any) => item.id === profile.id || item.api_base === profile.api_base); await persist({ ...payload.value, provider_configs: [...(payload.value.provider_configs ?? []).filter((item: any) => item.id !== profile.id && item.api_base !== profile.api_base), recordFrom(profile, existing ?? { id: profile.id })] }); }
  async function setDefault(service: any) { const { provider_configs: _legacyProviders, ...defaults } = payload.default_value ?? {}; await api('/settings/defaults.tts', { method: 'PUT', headers: { 'If-Match': `"${payload.default_revision}"` }, body: JSON.stringify({ value: { ...defaults, service: service.id } }) }); await load(); }
  const isDefault = (service: any) => [service.id, service.name].map((value) => String(value ?? '').toLowerCase()).includes(String(payload.default_service ?? '').toLowerCase());
  async function removeService(service: any) { await persist({ ...payload.value, provider_configs: (payload.value.provider_configs ?? []).filter((item: any) => item.id !== service.id && item.api_base !== service.api_base) }); }
  async function refreshService(service: any) { refreshing = service.id; error = ''; try { const found = await api<any>('/services/tts/discover', { method: 'POST', body: JSON.stringify({ base_url: service.api_base }) }); const existing = configuredRecord(service) ?? { id: service.id }; await persist({ ...payload.value, provider_configs: [...(payload.value.provider_configs ?? []).filter((item: any) => item.id !== existing.id && item.api_base !== service.api_base), recordFrom(found, existing)] }); } catch (caught) { error = caught instanceof Error ? caught.message : String(caught); } finally { refreshing = ''; } }

  load();
</script>

<section>
  <div class="flex flex-wrap items-end justify-between gap-4"><div><div class="eyebrow">TTS services</div><h2 class="mt-1 text-2xl font-semibold">Speech connections</h2><p class="muted mt-2 max-w-2xl text-sm">Manage endpoints, provider defaults, models, and pre-built voices here. Session workspaces can override model and voice choices without changing these defaults.</p></div><div class="flex gap-2"><button onclick={load} class="btn btn-secondary"><RefreshCw size={16}/> Refresh all</button><button onclick={() => discoverOpen = true} class="btn btn-primary"><Plus size={16}/> Connect endpoint</button></div></div>
  {#if error}<p class="mt-3 rounded-xl border border-red-400/40 bg-red-500/10 p-3 text-sm text-red-600">{error}</p>{/if}
  <div class="mt-5 grid gap-3 md:grid-cols-2">
    {#each payload.services ?? [] as service}
      <article class="rounded-2xl border border-[var(--line)] p-4"><div class="flex items-center gap-3"><div class="grid size-9 place-items-center rounded-xl bg-[var(--accent-soft)] text-[var(--accent)]"><Server size={17}/></div><div class="min-w-0 flex-1"><div class="truncate font-semibold">{service.name}</div><div class="muted truncate text-xs">{service.api_base}</div></div>{#if service.models?.length}<CheckCircle2 class="text-[var(--success)]" size={17}/>{/if}</div><div class="muted mt-3 text-xs">{service.models?.length ?? 0} models · {service.voices?.length ?? 0} voices{service.default_model ? ` · default ${service.default_model}` : ''}</div><div class="mt-3 flex flex-wrap gap-2"><button class="btn btn-sm btn-secondary" onclick={() => openSettings(service)}><Settings2 size={13}/> Settings & voices</button><button class="btn btn-sm btn-secondary" onclick={() => setDefault(service)} disabled={isDefault(service)}>{isDefault(service) ? 'Default' : 'Make default'}</button><button class="btn btn-sm btn-secondary" onclick={() => refreshService(service)} disabled={refreshing === service.id}><RefreshCw size={13}/>{refreshing === service.id ? 'Testing…' : 'Test & refresh'}</button>{#if configuredRecord(service)}<button class="btn btn-sm btn-secondary text-red-500" onclick={() => removeService(service)}>Remove</button>{/if}</div></article>
    {/each}
  </div>
  <details class="mt-6 rounded-2xl border border-[var(--line)] p-4"><summary class="cursor-pointer font-semibold">Known compatible endpoint profiles</summary><div class="mt-4 grid gap-2 md:grid-cols-2">{#each payload.profiles ?? [] as profile}<div class="rounded-xl border border-[var(--line)] p-3"><div class="text-sm font-semibold">{profile.name}</div><div class="muted mt-1 text-xs">{profile.description}</div><div class="mt-3 flex gap-2"><button onclick={() => useProfile(profile)} class="btn btn-sm btn-secondary">Use profile</button>{#if profile.source_url}<a href={profile.source_url} target="_blank" rel="noreferrer" class="btn btn-sm btn-quiet">Project <ExternalLink size={12}/></a>{/if}</div></div>{/each}</div></details>
</section>

{#if editing}
  <div class="fixed inset-0 z-50 grid place-items-center bg-black/40 p-5"><div class="surface max-h-[94vh] w-full max-w-4xl overflow-y-auto rounded-3xl p-7" role="dialog" aria-modal="true" aria-labelledby="service-settings-title"><header class="flex items-start justify-between gap-4"><div><div class="eyebrow">TTS service</div><h2 id="service-settings-title" class="mt-1 text-2xl font-semibold">{editing.name}</h2><p class="muted mt-2 text-sm">Connection, provider defaults, and per-model pre-built voice catalogues.</p></div><button onclick={() => editing = null} class="btn btn-icon btn-secondary" aria-label="Close service settings"><X size={18}/></button></header>
    <div class="mt-6 grid gap-5 lg:grid-cols-2"><section class="rounded-2xl border border-[var(--line)] p-4"><h3 class="font-semibold">Connection and defaults</h3><label class="mt-4 block text-sm font-semibold">Base URL<input bind:value={editing.api_base} class="field"/></label><label class="mt-4 block text-sm font-semibold">Default model{#if modelChoices.length}<select value={selectedModel} onchange={(event) => setDefaultModel(event.currentTarget.value)} class="field">{#each modelChoices as model}<option value={model}>{model}</option>{/each}</select>{:else}<input value={selectedModel} oninput={(event) => setDefaultModel(event.currentTarget.value)} placeholder="Model ID" class="field"/>{/if}</label>{#if settingKeys.length}<details class="mt-5 border-t border-[var(--line)] pt-4" open><summary class="cursor-pointer text-sm font-semibold">Provider defaults</summary><div class="mt-4 grid gap-3 sm:grid-cols-2">{#each settingKeys as key}<SettingField section="tts" keyName={key} value={editing.settings?.[key] ?? payload.builtin_defaults?.[key]} onchange={(value) => setServiceSetting(key, value)} compact/>{/each}</div></details>{/if}</section>
      <section class="rounded-2xl border border-[var(--line)] p-4"><h3 class="font-semibold">Pre-built voices by model</h3><p class="muted mt-1 text-xs">The selected default is used when a session does not choose a voice explicitly.</p>{#if modelChoices.length > 1}<label class="mt-4 block text-sm font-semibold">Voice catalogue for<select bind:value={selectedModel} class="field">{#each modelChoices as model}<option value={model}>{model}</option>{/each}</select></label>{/if}<label class="mt-4 block text-sm font-semibold">Default voice<select value={activeDefaultVoice} onchange={(event) => setDefaultVoice(event.currentTarget.value)} class="field"><option value="">No provider default</option>{#each voiceCatalogue as voice}<option value={voice}>{voice}</option>{/each}</select></label><div class="mt-4 flex gap-2"><input bind:value={newVoice} onkeydown={(event) => { if (event.key === 'Enter') { event.preventDefault(); addVoice(); } }} placeholder="Add a pre-built voice" class="field mt-0 min-w-0 flex-1"/><button onclick={addVoice} class="btn btn-secondary"><Plus size={14}/> Add</button></div><div class="mt-3 flex max-h-36 flex-wrap gap-2 overflow-auto">{#each voiceCatalogue as voice}<span class="inline-flex items-center gap-1 rounded-full bg-[var(--accent-soft)] px-2.5 py-1 text-xs font-semibold">{voice}{#if voice === activeDefaultVoice}<span class="text-[.6rem] uppercase text-[var(--accent)]">default</span>{/if}<button onclick={() => removeVoice(voice)} aria-label={`Remove ${voice}`} class="ml-1"><Trash2 size={12}/></button></span>{:else}<span class="muted text-xs">No pre-built voices recorded for this model.</span>{/each}</div><div class="mt-5 border-t border-[var(--line)] pt-4"><label class="text-sm font-semibold">Preview text<textarea bind:value={previewText} rows="3" maxlength="1000" class="field resize-y"></textarea></label><button onclick={previewVoice} disabled={previewing || !activeDefaultVoice} class="btn btn-primary mt-3"><Play size={14}/>{previewing ? 'Generating preview…' : 'Preview selected voice'}</button>{#if previewArtifactId}<audio controls autoplay src={`/api/v1/artifacts/${previewArtifactId}/content`} class="mt-3 h-10 w-full"></audio>{/if}</div></section></div>
    <footer class="mt-6 flex justify-end gap-2"><button onclick={() => editing = null} class="btn btn-secondary">Cancel</button><button onclick={() => persistEditing()} class="btn btn-primary">Save service settings</button></footer>
  </div></div>
{/if}

{#if discoverOpen}
  <div class="fixed inset-0 z-50 grid place-items-center bg-black/40 p-5"><div class="surface w-full max-w-xl rounded-3xl p-7"><div class="flex justify-between"><div><div class="eyebrow">Connect TTS</div><h2 class="mt-1 text-2xl font-semibold">Discover an endpoint</h2></div><button onclick={() => discoverOpen = false} aria-label="Close endpoint discovery" class="btn btn-icon btn-secondary"><X size={19}/></button></div><label class="mt-6 block text-sm font-semibold">Base URL<input bind:value={baseUrl} class="field"/></label><button onclick={discover} disabled={discovering} class="btn btn-primary mt-4 w-full">{discovering ? 'Inspecting…' : 'Detect API'}</button>{#if result}<div class="mt-5 rounded-xl border border-[var(--line)] p-4"><div class="font-semibold">{result.name}</div><p class="muted mt-2 text-sm">{result.message}</p><div class="muted mt-2 text-xs">{result.models?.length ?? 0} models · {result.voices?.length ?? 0} voices · {result.confidence} confidence</div><button onclick={saveDiscovered} class="btn btn-primary mt-4">Save connection</button></div>{/if}</div></div>
{/if}

<style>.field{margin-top:.4rem;width:100%;border:1px solid var(--line);border-radius:.72rem;background:var(--paper);padding:.65rem .72rem;font-weight:400;color:var(--ink)}</style>
