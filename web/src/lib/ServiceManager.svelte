<script lang="ts">
  import { CheckCircle2, ExternalLink, Library, Plus, RefreshCw, Server, Settings2, X } from '@lucide/svelte';
  import { credentialApi, settingApi, speechServiceApi } from './admin-api';
  import type { TtsCatalogue, TtsDiscovery, TtsService, TtsSettingsValue } from './api-models';
  import SettingField from './SettingField.svelte';
  import CredentialStorageFields, { type CredentialBackendProfile } from './CredentialStorageFields.svelte';

  const SERVICE_SETTING_KEYS: Record<string, string[]> = {
    xtts: ['temperature', 'length_penalty', 'repetition_penalty', 'top_k', 'top_p', 'do_sample', 'num_beams', 'enable_text_splitting', 'stream_chunk_size', 'gpt_cond_len', 'gpt_cond_chunk_len', 'max_ref_len', 'sound_norm_refs', 'overlap_wav_len'],
    voxcpm: ['voxcpm_cfg_value', 'voxcpm_inference_timesteps', 'voxcpm_normalize', 'voxcpm_denoise', 'voxcpm_retry_badcase', 'voxcpm_retry_badcase_max_times', 'voxcpm_retry_badcase_ratio_threshold', 'voxcpm_min_len', 'voxcpm_max_len'],
    fishs2: ['fishs2_temperature', 'fishs2_top_p', 'fishs2_chunk_length', 'fishs2_latency', 'fishs2_normalize', 'fishs2_prosody_volume', 'fishs2_normalize_loudness'],
    voxtral: ['voxtral_max_frames', 'voxtral_euler_steps', 'voxtral_chunk', 'voxtral_max_chunk_chars', 'voxtral_chunk_silence_ms', 'voxtral_strip_quotes', 'voxtral_strip_diacritics', 'voxtral_level_audio'],
    chatterbox: ['chatterbox_temperature', 'chatterbox_repetition_penalty', 'chatterbox_min_p', 'chatterbox_top_p', 'chatterbox_top_k', 'chatterbox_exaggeration', 'chatterbox_cfg_weight', 'chatterbox_norm_loudness'],
    silero: ['silero_stress_mode', 'silero_sample_rate'],
    openai: ['openai_audio_instructions'], gemini: ['openai_audio_instructions']
  };

  type ServicePayload = TtsCatalogue & {
    services: TtsService[];
    profiles: TtsService[];
    value: TtsSettingsValue;
    revision: number;
    default_value: Record<string, unknown>;
    default_service: string;
    default_revision: number;
    builtin_defaults: Record<string, unknown>;
  };
  type CredentialBackend = 'database' | 'environment' | 'keyring' | 'file';

  let payload = $state<ServicePayload>({ services: [], profiles: [], value: {}, revision: 0, default_value: {}, default_service: 'XTTS', default_revision: 0, builtin_defaults: {} });
  let credentialBackends = $state<CredentialBackendProfile[]>([]);
  let discoverOpen = $state(false);
  let baseUrl = $state('http://127.0.0.1:8000');
  let discoveryApiKey = $state('');
  let discovering = $state(false);
  let refreshing = $state('');
  let result = $state<TtsDiscovery | null>(null);
  let error = $state('');
  let editing = $state<TtsService | null>(null);
  let editingApiKey = $state('');
  let editingCredentialBackend = $state<CredentialBackend>('database');
  let editingExistingCredentialBackend = $state<CredentialBackend>('database');
  let editingCredentialReference = $state('');
  let deletePreviousCredential = $state(false);
  let editingModelsText = $state('');
  let removeEditingApiKey = $state(false);
  let selectedModel = $state('');

  const normalizedId = (service: TtsService | null) => String(service?.id ?? '').toLowerCase().replaceAll('-', '_');
  const settingKeys = $derived(SERVICE_SETTING_KEYS[normalizedId(editing)] ?? []);
  const modelChoices = $derived(Array.from(new Set([
    ...editingModelsText.split(/[\n,;]+/).map((value) => value.trim()).filter(Boolean),
    editing?.default_model
  ].filter(Boolean))));

  async function load() {
    const [servicePayload, backendPayload] = await Promise.all([
      speechServiceApi.catalogueAs<ServicePayload>(true),
      credentialApi.backends<CredentialBackendProfile>()
    ]);
    payload = servicePayload;
    credentialBackends = backendPayload.items;
  }
  async function persist(value: TtsSettingsValue) {
    await settingApi.put<TtsSettingsValue>('services.tts', payload.revision, value);
    await load();
  }

  function recordFrom(candidate: Partial<TtsService>, existing: Partial<TtsService> = {}): TtsService {
    const id = existing.id ?? candidate.id ?? String(candidate.name ?? 'service').toLowerCase().replace(/[^a-z0-9]+/g, '_');
    return {
      ...existing,
      id,
      name: candidate.name ?? existing.name ?? id, kind: candidate.kind, api_base: candidate.api_base, provider: candidate.provider,
      adapter: candidate.adapter, speech_path: candidate.speech_path, models_path: candidate.models_path,
      voices_path: candidate.voices_path, request_fields: candidate.request_fields, request_defaults: candidate.request_defaults,
      auth_mode: candidate.auth_mode ?? existing.auth_mode, direct_http: candidate.direct_http ?? existing.direct_http,
      vertex_project: candidate.vertex_project ?? existing.vertex_project, vertex_location: candidate.vertex_location ?? existing.vertex_location,
      models: candidate.models ?? [], voices: candidate.voices ?? [], default_model: candidate.default_model,
      default_voice: candidate.default_voice, voice_catalogues: candidate.voice_catalogues ?? existing.voice_catalogues ?? {},
      default_voices: candidate.default_voices ?? existing.default_voices ?? {},
      default_voices_by_language: candidate.default_voices_by_language ?? existing.default_voices_by_language ?? {},
      settings: candidate.settings ?? existing.settings ?? {}, supports_prebuilt_voices: candidate.supports_prebuilt_voices,
      secret_ref: existing.secret_ref ?? candidate.secret_ref, api_key_env: candidate.api_key_env ?? existing.api_key_env,
      credential_configured: candidate.credential_configured ?? existing.credential_configured ?? false,
      credential_source: candidate.credential_source ?? existing.credential_source ?? 'none',
      credential_backend: candidate.credential_backend ?? existing.credential_backend,
      credential_reference: candidate.credential_reference ?? existing.credential_reference
    };
  }

  const configuredRecord = (service: TtsService) => (payload.value.provider_configs ?? []).find((item) => item.id === service.id || item.api_base === service.api_base);
  const isDefault = (service: TtsService) => [service.id, service.name].map((value) => String(value ?? '').toLowerCase()).includes(String(payload.default_service ?? '').toLowerCase());

  function openSettings(service: TtsService) {
    const saved = configuredRecord(service) ?? {};
    editing = recordFrom({ ...service, ...saved }, saved);
    editingApiKey = '';
    editingCredentialBackend = editing.credential_backend ?? (String(editing.secret_ref ?? '').startsWith('env:') ? 'environment' : String(editing.secret_ref ?? '').startsWith('keyring:') ? 'keyring' : String(editing.secret_ref ?? '').startsWith('file') ? 'file' : 'database');
    editingExistingCredentialBackend = editingCredentialBackend;
    editingCredentialReference = editing.credential_reference ?? (String(editing.secret_ref ?? '').startsWith('env:') ? String(editing.secret_ref).slice(4) : '');
    deletePreviousCredential = false;
    editingModelsText = (editing.models ?? []).join('\n');
    removeEditingApiKey = false;
    selectedModel = editing.default_model || editing.models?.[0] || '';
    if (selectedModel && !editing.models?.includes(selectedModel)) editing.models = [selectedModel, ...(editing.models ?? [])];
    editing = { ...editing, settings: { ...(editing.settings ?? {}) } };
    error = '';
  }

  function setServiceSetting(key: string, value: unknown) { if (editing) editing = { ...editing, settings: { ...(editing.settings ?? {}), [key]: value } }; }
  function setDefaultModel(value: string) {
    selectedModel = value;
    if (editing) editing = { ...editing, default_model: value, models: Array.from(new Set([...(editing.models ?? []), value].filter(Boolean))) };
  }

  async function persistEditing() {
    if (!editing) return;
    const manualModels = Array.from(new Set(editingModelsText.split(/[\n,;]+/).map((value) => value.trim()).filter(Boolean)));
    const defaultModel = editing.default_model || selectedModel || manualModels[0] || '';
    const models = Array.from(new Set([defaultModel, ...manualModels].filter(Boolean)));
    const baseRecord = recordFrom({ ...editing, models, default_model: defaultModel }, editing);
    delete baseRecord.credential_backend;
    delete baseRecord.credential_reference;
    const credential = removeEditingApiKey ? { clear_api_key: true } : {
      credential_backend: editingCredentialBackend,
      credential_reference: editingCredentialReference.trim() || null,
      delete_previous_credential: deletePreviousCredential,
      ...(editingApiKey.trim() ? { api_key: editingApiKey.trim() } : {})
    };
    const record = { ...baseRecord, ...credential };
    await persist({ ...payload.value, provider_configs: [...(payload.value.provider_configs ?? []).filter((item) => item.id !== record.id && item.api_base !== record.api_base), record] });
    editing = null;
  }

  async function discover() {
    discovering = true; error = '';
    try { result = await speechServiceApi.discover({ base_url: baseUrl, ...(discoveryApiKey.trim() ? { api_key: discoveryApiKey.trim() } : {}) }); }
    catch (caught) { error = caught instanceof Error ? caught.message : String(caught); }
    finally { discovering = false; }
  }
  async function saveDiscovered() {
    if (!result?.success) return;
    const existing = (payload.value.provider_configs ?? []).find((item) => item.api_base === result?.api_base);
    const record = { ...recordFrom(result, existing), ...(discoveryApiKey.trim() ? { api_key: discoveryApiKey.trim() } : {}) };
    await persist({ ...payload.value, provider_configs: [...(payload.value.provider_configs ?? []).filter((item) => item.api_base !== result?.api_base), record] });
    discoverOpen = false; result = null; discoveryApiKey = '';
  }
  async function useProfile(profile: TtsService) {
    const existing = (payload.value.provider_configs ?? []).find((item) => item.id === profile.id || item.api_base === profile.api_base);
    await persist({ ...payload.value, provider_configs: [...(payload.value.provider_configs ?? []).filter((item) => item.id !== profile.id && item.api_base !== profile.api_base), recordFrom(profile, existing ?? { id: profile.id })] });
  }
  async function setDefault(service: TtsService) {
    const { provider_configs: _legacyProviders, ...defaults } = payload.default_value ?? {};
    await settingApi.put<Record<string, unknown>>('defaults.tts', payload.default_revision, { ...defaults, service: service.id });
    await load();
  }
  async function removeService(service: TtsService) { await persist({ ...payload.value, provider_configs: (payload.value.provider_configs ?? []).filter((item) => item.id !== service.id && item.api_base !== service.api_base) }); }
  async function refreshService(service: TtsService) {
    refreshing = service.id; error = '';
    try {
      const found = await speechServiceApi.discover({ base_url: service.api_base ?? '', service_id: service.id });
      const existing = configuredRecord(service) ?? { id: service.id };
      await persist({ ...payload.value, provider_configs: [...(payload.value.provider_configs ?? []).filter((item) => item.id !== existing.id && item.api_base !== service.api_base), recordFrom(found, existing)] });
    } catch (caught) { error = caught instanceof Error ? caught.message : String(caught); }
    finally { refreshing = ''; }
  }

  load();
</script>

<section>
  <div class="flex flex-wrap items-end justify-between gap-4"><div><div class="eyebrow">TTS services</div><h2 class="mt-1 text-2xl font-semibold">Speech connections</h2><p class="muted mt-2 max-w-2xl text-sm">Manage endpoints, API keys, provider defaults, and model catalogues here. Browse and preview pre-built voices in the Voice Library.</p></div><div class="flex flex-wrap gap-2"><a href="/voices?view=prebuilt" class="btn btn-secondary"><Library size={15}/> Open Voice Library</a><button onclick={load} class="btn btn-secondary"><RefreshCw size={16}/> Refresh all</button><button onclick={() => { discoverOpen = true; discoveryApiKey = ''; result = null; }} class="btn btn-primary"><Plus size={16}/> Connect endpoint</button></div></div>
  {#if error}<p class="mt-3 rounded-xl border border-red-400/40 bg-red-500/10 p-3 text-sm text-red-600">{error}</p>{/if}
  <div class="mt-5 grid gap-3 md:grid-cols-2">
    {#each payload.services ?? [] as service}
      <article class="rounded-2xl border border-[var(--line)] p-4"><div class="flex items-center gap-3"><div class="grid size-9 place-items-center rounded-xl bg-[var(--accent-soft)] text-[var(--accent)]"><Server size={17}/></div><div class="min-w-0 flex-1"><div class="flex flex-wrap items-center gap-2"><div class="truncate font-semibold">{service.name}</div><span class="rounded-full border border-[var(--line)] px-2 py-0.5 text-[.62rem] font-bold uppercase">{service.credential_configured ? `Key: ${service.credential_source}` : 'No key'}</span></div><div class="muted truncate text-xs">{service.api_base}</div></div>{#if service.models?.length}<CheckCircle2 class="text-[var(--success)]" size={17}/>{/if}</div><div class="muted mt-3 text-xs">{service.models?.length ?? 0} models · {service.voices?.length ?? 0} voices{service.default_model ? ` · default ${service.default_model}` : ''}</div><div class="mt-3 flex flex-wrap gap-2"><button class="btn btn-sm btn-secondary" onclick={() => openSettings(service)}><Settings2 size={13}/> Service settings</button>{#if service.supports_prebuilt_voices}<a href={`/voices?view=prebuilt&service=${encodeURIComponent(service.id)}`} class="btn btn-sm btn-secondary"><Library size={13}/> Preview voices</a>{/if}<button class="btn btn-sm btn-secondary" onclick={() => setDefault(service)} disabled={isDefault(service)}>{isDefault(service) ? 'Default' : 'Make default'}</button>{#if !service.direct_http && normalizedId(service) !== 'vertex_ai'}<button class="btn btn-sm btn-secondary" onclick={() => refreshService(service)} disabled={refreshing === service.id}><RefreshCw size={13}/>{refreshing === service.id ? 'Testing…' : 'Test & refresh'}</button>{/if}{#if configuredRecord(service)}<button class="btn btn-sm btn-secondary text-red-500" onclick={() => removeService(service)}>Remove</button>{/if}</div></article>
    {/each}
  </div>
  <details class="mt-6 rounded-2xl border border-[var(--line)] p-4"><summary class="cursor-pointer font-semibold">Known compatible endpoint profiles</summary><div class="mt-4 grid gap-2 md:grid-cols-2">{#each payload.profiles ?? [] as profile}<div class="rounded-xl border border-[var(--line)] p-3"><div class="text-sm font-semibold">{profile.name}</div><div class="muted mt-1 text-xs">{profile.description}</div><div class="mt-3 flex gap-2"><button onclick={() => useProfile(profile)} class="btn btn-sm btn-secondary">Use profile</button>{#if profile.source_url}<a href={profile.source_url} target="_blank" rel="noreferrer" class="btn btn-sm btn-quiet">Project <ExternalLink size={12}/></a>{/if}</div></div>{/each}</div></details>
</section>

{#if editing}
  <div class="fixed inset-0 z-50 grid place-items-center bg-black/40 p-2 sm:p-5"><div class="surface modal-panel flex w-full max-w-3xl flex-col" role="dialog" aria-modal="true" aria-labelledby="service-settings-title"><header class="flex shrink-0 items-start justify-between gap-4 border-b border-[var(--line)] px-5 py-4 sm:px-7"><div><div class="eyebrow">TTS service</div><h2 id="service-settings-title" class="mt-1 text-2xl font-semibold">{editing.name}</h2><p class="muted mt-2 text-sm">Connection, model, and generation defaults.</p></div><button onclick={() => editing = null} class="btn btn-icon btn-secondary" aria-label="Close service settings"><X size={18}/></button></header>
    <div class="modal-scroll p-5 sm:p-7"><section class="rounded-2xl border border-[var(--line)] p-4 sm:p-5"><h3 class="font-semibold">Connection and defaults</h3><label class="mt-4 block text-sm font-semibold">Base URL<input bind:value={editing.api_base} class="field"/></label>{#if normalizedId(editing) === 'vertex_ai'}<div class="mt-4 grid gap-3 sm:grid-cols-2"><label class="block text-sm font-semibold">Google Cloud project<input bind:value={editing.vertex_project} placeholder="Uses project_id from JSON when blank" class="field"/></label><label class="block text-sm font-semibold">Vertex location<input bind:value={editing.vertex_location} placeholder="us-central1" class="field"/></label></div>{/if}<div class="mt-4"><CredentialStorageFields backends={credentialBackends} bind:backend={editingCredentialBackend} bind:reference={editingCredentialReference} bind:secret={editingApiKey} bind:deletePrevious={deletePreviousCredential} configured={Boolean(editing.credential_configured)} currentSource={editing.credential_source ?? 'none'} existingBackend={editingExistingCredentialBackend} suggestedEnvironment={editing.api_key_env ?? ''} secretLabel={normalizedId(editing) === 'vertex_ai' ? 'Google service-account JSON' : 'API key'} multiline={normalizedId(editing) === 'vertex_ai'}/></div>{#if editing.credential_configured || editing.secret_ref}<label class="mt-3 flex items-center gap-2 text-sm"><input type="checkbox" bind:checked={removeEditingApiKey} onchange={() => { if (removeEditingApiKey) { editingApiKey = ''; deletePreviousCredential = false; } }} class="accent-[var(--accent)]"/> Remove this service's credential reference{#if ['database', 'keyring'].includes(editing.credential_source ?? '')} and stored value{/if}</label>{/if}<label class="mt-4 block text-sm font-semibold">Models / deployment names<textarea bind:value={editingModelsText} rows="4" spellcheck="false" placeholder="One model or Azure deployment name per line" class="field font-mono text-xs"></textarea><small class="muted mt-1 block font-normal">Enter one ID per line. Azure deployment names are kept exactly as entered.</small></label><label class="mt-4 block text-sm font-semibold">Default model{#if modelChoices.length}<select value={selectedModel} onchange={(event) => setDefaultModel(event.currentTarget.value)} class="field">{#each modelChoices as model}<option value={model}>{model}</option>{/each}</select>{:else}<input value={selectedModel} oninput={(event) => setDefaultModel(event.currentTarget.value)} placeholder="Model ID" class="field"/>{/if}</label>{#if settingKeys.length}<details class="mt-5 border-t border-[var(--line)] pt-4" open><summary class="cursor-pointer text-sm font-semibold">Provider defaults</summary><div class="provider-default-grid mt-4">{#each settingKeys as key}<SettingField section="tts" keyName={key} value={editing.settings?.[key] ?? payload.builtin_defaults?.[key]} onchange={(value) => setServiceSetting(key, value)} compact/>{/each}</div></details>{/if}</section>
      {#if editing.supports_prebuilt_voices}<a href={`/voices?view=prebuilt&service=${encodeURIComponent(editing.id)}`} class="mt-5 flex items-center gap-3 rounded-2xl border border-[var(--line)] bg-[var(--accent-soft)] p-4"><Library class="shrink-0 text-[var(--accent)]" size={20}/><span class="min-w-0 flex-1"><strong class="block text-sm">Voice preview is in the Voice Library</strong><span class="muted mt-1 block text-xs">Browse by language, compare samples, and choose defaults there.</span></span><ExternalLink size={15}/></a>{/if}
    </div>
    <footer class="flex shrink-0 justify-end gap-2 border-t border-[var(--line)] px-5 py-4 sm:px-7"><button onclick={() => editing = null} class="btn btn-secondary">Cancel</button><button onclick={persistEditing} class="btn btn-primary">{editingCredentialBackend !== editingExistingCredentialBackend ? 'Verify move and save' : 'Save service settings'}</button></footer>
  </div></div>
{/if}

{#if discoverOpen}
  <div class="fixed inset-0 z-50 grid place-items-center bg-black/40 p-2 sm:p-5"><div class="surface modal-panel flex w-full max-w-xl flex-col"><div class="modal-scroll p-5 sm:p-7"><div class="flex justify-between gap-4"><div><div class="eyebrow">Connect TTS</div><h2 class="mt-1 text-2xl font-semibold">Discover an endpoint</h2></div><button onclick={() => discoverOpen = false} aria-label="Close endpoint discovery" class="btn btn-icon btn-secondary"><X size={19}/></button></div><label class="mt-6 block text-sm font-semibold">Base URL<input bind:value={baseUrl} class="field"/></label><label class="mt-4 block text-sm font-semibold">API key<input bind:value={discoveryApiKey} type="password" autocomplete="new-password" placeholder="Optional API key" class="field"/><small class="muted mt-1 block font-normal">Used for discovery, then saved only after you choose Save connection.</small></label><button onclick={discover} disabled={discovering} class="btn btn-primary mt-4 w-full">{discovering ? 'Inspecting…' : 'Detect API'}</button>{#if result}<div class="mt-5 rounded-xl border border-[var(--line)] p-4"><div class="font-semibold">{result.name}</div><p class="muted mt-2 text-sm">{result.message}</p><div class="muted mt-2 text-xs">{result.models?.length ?? 0} models · {result.voices?.length ?? 0} voices · {result.confidence} confidence</div><button onclick={saveDiscovered} class="btn btn-primary mt-4">Save connection</button></div>{/if}</div></div></div>
{/if}

<style>
  .field{margin-top:.4rem;width:100%;min-width:0;border:1px solid var(--line);border-radius:.72rem;background:var(--paper);padding:.65rem .72rem;font-weight:400;color:var(--ink)}
  .provider-default-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:.8rem}
  @media(max-width:620px){.provider-default-grid{grid-template-columns:minmax(0,1fr)}}
</style>
