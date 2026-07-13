<script lang="ts">
  import { ArrowLeft, Bot, CheckCircle2, ChevronDown, CircleAlert, Pencil, Plus, RefreshCw, Settings2, Trash2, X } from '@lucide/svelte';
  import { api } from './api';
  import { onMount } from 'svelte';
  import GuidedTour from './GuidedTour.svelte';

  type Provider = { id: string; provider_key: string; label: string; base_url?: string; secret_ref?: string; enabled: boolean; options_json: Record<string, any>; revision: number };
  type ProviderProfile = { id: string; label: string; provider_key: string; base_url: string; secret_ref: string; description: string; options?: Record<string, any> };
  type Model = { id: string; provider_id: string; model_id: string; is_default: boolean; default_temperature: number | null; default_reasoning_effort: string | null; input_cost_per_million: number | null; cached_input_cost_per_million: number | null; output_cost_per_million: number | null; options_json: Record<string, any>; revision: number };
  type RequestOptionRow = { id: string; key: string; value: string; type: 'text' | 'number' | 'boolean' };

  let { onback }: { onback: () => void } = $props();
  let providers = $state<Provider[]>([]);
  let profiles = $state<ProviderProfile[]>([]);
  let models = $state<Record<string, Model[]>>({});
  let error = $state('');
  let notice = $state('');
  let loading = $state(false);
  let tourOpen = $state(false);

  let providerModal = $state(false);
  let editingProvider = $state<Provider | null>(null);
  let providerProfileId = $state('custom-openai');
  let providerLabel = $state('');
  let providerKey = $state('openai');
  let providerUrl = $state('');
  let providerSecretRef = $state('');
  let providerEnabled = $state(true);
  let providerMetadata = $state<Record<string, any>>({});
  let requestOptionRows = $state<RequestOptionRow[]>([]);

  let addModelProvider = $state<string | null>(null);
  let modelId = $state('');
  let edit = $state<Model | null>(null);
  let temperature = $state('');
  let reasoning = $state('');
  let inputCost = $state('');
  let cachedCost = $state('');
  let outputCost = $state('');
  let deletingModel = $state<Model | null>(null);
  let deletingProvider = $state<Provider | null>(null);
  let replacementModelRecordId = $state('');

  const allModels = $derived(Object.values(models).flat());
  const selectedProviderProfile = $derived(profiles.find((item) => item.id === providerProfileId));
  const providerKeys = ['openai', 'anthropic', 'gemini', 'openrouter', 'ollama', 'groq', 'mistral', 'vertex_ai', 'azure', 'bedrock'];
  const requestOptionKeys = ['organization', 'project', 'api_version', 'location', 'region_name', 'aws_region_name', 'azure_ad_token', 'deployment_id'];
  const tourSteps = [
    { section: 'Connections', title: 'Profiles are editable starting points', body: 'Choose a LiteLLM profile, then adjust its display name, provider adapter, URL, credential reference, and advanced request options.' },
    { section: 'Models', title: 'One record per canonical model', body: 'Discovery adds new IDs without removing manual models or their temperature, reasoning, and cost settings.' },
    { section: 'Defaults', title: 'One application default', body: 'The active default applies across providers. Blank optional values are omitted; zero temperature is sent explicitly.' }
  ];

  function report(caught: unknown) {
    error = caught instanceof Error ? caught.message : String(caught);
    notice = '';
  }

  async function load() {
    loading = true;
    try {
      const [providerResult, profileResult] = await Promise.all([
        api<{items: Provider[]}>('/providers'),
        api<{items: ProviderProfile[]}>('/providers/profiles')
      ]);
      providers = providerResult.items;
      profiles = profileResult.items;
      const entries = await Promise.all(providers.map(async (provider) => [provider.id, (await api<{items: Model[]}>(`/providers/${provider.id}/models`)).items] as const));
      models = Object.fromEntries(entries);
      error = '';
    } catch (caught) {
      report(caught);
    } finally {
      loading = false;
    }
  }

  function applyProfile(profileId: string) {
    providerProfileId = profileId;
    const profile = profiles.find((item) => item.id === profileId);
    if (!profile) return;
    providerLabel = profile.label;
    providerKey = profile.provider_key;
    providerUrl = profile.base_url ?? '';
    providerSecretRef = profile.secret_ref ?? '';
    setStructuredOptions({ ...(profile.options ?? {}), profile_id: profile.id });
  }

  const rowId = () => Math.random().toString(36).slice(2);
  function setStructuredOptions(options: Record<string, any>) {
    const requestOptions = options.request_options && typeof options.request_options === 'object' ? options.request_options : {};
    providerMetadata = Object.fromEntries(Object.entries(options).filter(([key]) => !['request_options', 'profile_id'].includes(key)));
    requestOptionRows = Object.entries(requestOptions).map(([key, value]) => ({ id: rowId(), key, value: String(value ?? ''), type: typeof value === 'boolean' ? 'boolean' : typeof value === 'number' ? 'number' : 'text' }));
  }

  function addRequestOption() { requestOptionRows = [...requestOptionRows, { id: rowId(), key: '', value: '', type: 'text' }]; }
  function removeRequestOption(id: string) { requestOptionRows = requestOptionRows.filter((row) => row.id !== id); }
  function requestOptionsValue() {
    return Object.fromEntries(requestOptionRows.filter((row) => row.key.trim()).map((row) => [row.key.trim(), row.type === 'number' ? Number(row.value) : row.type === 'boolean' ? row.value === 'true' : row.value]));
  }

  function openNewProvider() {
    editingProvider = null;
    providerEnabled = true;
    applyProfile(profiles.some((item) => item.id === 'custom-openai') ? 'custom-openai' : profiles[0]?.id ?? '');
    providerModal = true;
  }

  function openProvider(provider: Provider) {
    editingProvider = provider;
    providerProfileId = String(provider.options_json?.profile_id ?? '');
    providerLabel = provider.label;
    providerKey = provider.provider_key;
    providerUrl = provider.base_url ?? '';
    providerSecretRef = provider.secret_ref ?? '';
    providerEnabled = provider.enabled;
    setStructuredOptions(provider.options_json ?? {});
    providerModal = true;
  }

  async function saveProvider() {
    if (!providerLabel.trim()) { error = 'A display name is required.'; return; }
    if (!providerKey.trim()) { error = 'A LiteLLM provider adapter is required.'; return; }
    const options: Record<string, any> = { ...providerMetadata, ...(providerProfileId ? { profile_id: providerProfileId } : {}), ...(requestOptionRows.some((row) => row.key.trim()) ? { request_options: requestOptionsValue() } : {}) };
    const body = JSON.stringify({ provider_key: providerKey.trim(), label: providerLabel.trim(), enabled: providerEnabled, base_url: providerUrl.trim() || null, secret_ref: providerSecretRef.trim() || null, options });
    try {
      if (editingProvider) {
        await api(`/providers/${editingProvider.id}`, { method: 'PATCH', headers: { 'If-Match': `"${editingProvider.revision}"` }, body });
        notice = `Updated ${providerLabel.trim()}.`;
      } else {
        await api('/providers', { method: 'POST', body });
        notice = `Added ${providerLabel.trim()}. Add or discover at least one model next.`;
      }
      providerModal = false;
      await load();
    } catch (caught) { report(caught); }
  }

  function openSettings(model: Model) {
    edit = model;
    temperature = model.default_temperature?.toString() ?? '';
    reasoning = model.default_reasoning_effort ?? '';
    inputCost = model.input_cost_per_million?.toString() ?? '';
    cachedCost = model.cached_input_cost_per_million?.toString() ?? '';
    outputCost = model.output_cost_per_million?.toString() ?? '';
  }

  const optionalNumber = (value: string) => value.trim() === '' ? null : Number(value);

  async function saveModel() {
    if (!edit) return;
    try {
      await api(`/providers/${edit.provider_id}/models/${edit.id}`, { method: 'PATCH', headers: { 'If-Match': `"${edit.revision}"` }, body: JSON.stringify({ default_temperature: optionalNumber(temperature), default_reasoning_effort: reasoning.trim() || null, input_cost_per_million: optionalNumber(inputCost), cached_input_cost_per_million: optionalNumber(cachedCost), output_cost_per_million: optionalNumber(outputCost) }) });
      edit = null;
      notice = 'Model defaults saved.';
      await load();
    } catch (caught) { report(caught); }
  }

  async function makeDefault(model: Model) {
    try {
      await api(`/providers/${model.provider_id}/models/${model.id}`, { method: 'PATCH', headers: { 'If-Match': `"${model.revision}"` }, body: JSON.stringify({ is_default: true }) });
      notice = `${model.model_id} is now the application default.`;
      await load();
    } catch (caught) { report(caught); }
  }

  async function createModel(providerId: string) {
    if (!modelId.trim()) { error = 'A canonical model ID is required.'; return; }
    try {
      await api(`/providers/${providerId}/models`, { method: 'POST', body: JSON.stringify({ model_id: modelId.trim(), is_default: !allModels.some((item) => item.is_default) }) });
      modelId = '';
      addModelProvider = null;
      notice = 'Model added.';
      await load();
    } catch (caught) { report(caught); }
  }

  function requestModelDelete(model: Model) {
    deletingModel = model;
    replacementModelRecordId = model.is_default ? allModels.find((item) => item.id !== model.id)?.id ?? '' : '';
  }

  async function removeModel() {
    if (!deletingModel) return;
    try {
      await api(`/providers/${deletingModel.provider_id}/models/${deletingModel.id}`, { method: 'DELETE', body: JSON.stringify({ replacement_model_record_id: replacementModelRecordId || null }) });
      deletingModel = null;
      notice = 'Model removed.';
      await load();
    } catch (caught) { report(caught); }
  }

  function requestProviderDelete(provider: Provider) {
    deletingProvider = provider;
    const ownsDefault = (models[provider.id] ?? []).some((item) => item.is_default);
    replacementModelRecordId = ownsDefault ? allModels.find((item) => item.provider_id !== provider.id)?.id ?? '' : '';
  }

  async function removeProvider() {
    if (!deletingProvider) return;
    try {
      await api(`/providers/${deletingProvider.id}`, { method: 'DELETE', body: JSON.stringify({ replacement_model_record_id: replacementModelRecordId || null }) });
      deletingProvider = null;
      notice = 'Provider removed.';
      await load();
    } catch (caught) { report(caught); }
  }

  async function refresh(providerId: string) {
    try {
      const result = await api<{added: string[]}>(`/providers/${providerId}/models/refresh`, { method: 'POST', body: '{}' });
      notice = result.added.length ? `Discovered ${result.added.length} new model${result.added.length === 1 ? '' : 's'}.` : 'Model catalogue is already up to date.';
      await load();
    } catch (caught) { report(caught); }
  }

  async function refreshAll() {
    for (const provider of providers.filter((item) => item.enabled)) await refresh(provider.id);
  }

  async function testProvider(providerId: string) {
    const selected = models[providerId]?.find((item) => item.is_default) ?? models[providerId]?.[0];
    if (!selected) { error = 'Add a model before testing this provider.'; return; }
    try {
      const result = await api<{model: string}>(`/providers/${providerId}/test`, { method: 'POST', body: JSON.stringify({ model_id: selected.model_id }) });
      error = '';
      notice = `Connection succeeded with ${result.model}.`;
    } catch (caught) { report(caught); }
  }

  onMount(load);
</script>

<div class="mx-auto max-w-6xl">
  <button onclick={onback} class="btn btn-quiet mb-6"><ArrowLeft size={16}/> Workspace</button>
  <header class="mb-7 flex flex-wrap items-end justify-between gap-5">
    <div><div class="eyebrow">Providers</div><h1 class="mt-2 text-4xl font-semibold">LLM connections and models</h1><p class="muted mt-3 max-w-3xl">Configure LiteLLM adapters, endpoints, credential references, catalogues, optional request parameters, and fallback pricing in one place.</p></div>
    <div class="flex flex-wrap gap-2"><button onclick={() => tourOpen = true} class="btn btn-secondary">Tour</button><button onclick={refreshAll} disabled={!providers.length || loading} class="btn btn-secondary"><RefreshCw size={16}/> Refresh catalogues</button><button onclick={openNewProvider} class="btn btn-primary"><Plus size={17}/> Add provider</button></div>
  </header>

  {#if error}<div role="alert" class="mb-4 flex items-start gap-2 rounded-xl border border-red-400/40 bg-red-500/10 px-4 py-3 text-sm"><CircleAlert class="mt-0.5 shrink-0" size={16}/><span>{error}</span></div>{/if}
  {#if notice}<div role="status" class="mb-4 flex items-center gap-2 rounded-xl border border-[var(--line)] bg-[var(--accent-soft)] px-4 py-3 text-sm"><CheckCircle2 size={16}/>{notice}</div>{/if}

  <div class="space-y-5">
    {#each providers as provider}
      <section class:opacity-60={!provider.enabled} class="surface overflow-hidden rounded-[1.5rem]">
        <header class="flex flex-wrap items-center justify-between gap-4 border-b border-[var(--line)] p-5">
          <div class="flex min-w-0 items-center gap-3"><div class="grid size-10 shrink-0 place-items-center rounded-xl bg-[var(--accent-soft)] text-[var(--accent)]"><Bot size={19}/></div><div class="min-w-0"><div class="flex flex-wrap items-center gap-2"><h2 class="font-semibold">{provider.label}</h2><span class="rounded-full bg-[var(--accent-soft)] px-2 py-0.5 text-[.65rem] font-bold uppercase text-[var(--accent)]">{provider.enabled ? 'Enabled' : 'Disabled'}</span></div><p class="muted mt-1 truncate text-xs">LiteLLM: {provider.provider_key}{provider.base_url ? ` · ${provider.base_url}` : ' · provider-managed endpoint'}</p></div></div>
          <div class="flex flex-wrap gap-2"><button onclick={() => testProvider(provider.id)} disabled={!provider.enabled || !(models[provider.id]?.length)} class="btn btn-sm btn-secondary">Test</button><button onclick={() => refresh(provider.id)} disabled={!provider.enabled} class="btn btn-sm btn-secondary"><RefreshCw size={14}/> Discover</button><button onclick={() => openProvider(provider)} class="btn btn-sm btn-secondary"><Pencil size={14}/> Edit</button><button onclick={() => { addModelProvider = provider.id; modelId = ''; }} class="btn btn-sm btn-secondary"><Plus size={14}/> Add model</button><button onclick={() => requestProviderDelete(provider)} aria-label={`Delete ${provider.label}`} class="btn btn-sm btn-secondary text-red-500"><Trash2 size={15}/></button></div>
        </header>
        <div>
          {#each models[provider.id] ?? [] as model}
            <div class="flex flex-wrap items-center gap-4 border-b border-[var(--line)] px-5 py-4 last:border-0">
              <div class="min-w-0 flex-1"><div class="flex items-center gap-2"><span class="truncate font-mono text-sm font-semibold">{model.model_id}</span>{#if model.is_default}<span class="rounded-full bg-[var(--accent-soft)] px-2 py-0.5 text-[.65rem] font-bold uppercase text-[var(--accent)]">Application default</span>{/if}</div><p class="muted mt-1 text-xs">Temperature {model.default_temperature ?? 'omit'} · Reasoning {model.default_reasoning_effort || 'omit'} · Input ${model.input_cost_per_million ?? '—'} / cached ${model.cached_input_cost_per_million ?? model.input_cost_per_million ?? '—'} / output ${model.output_cost_per_million ?? '—'}</p></div>
              <div class="flex gap-2">{#if !model.is_default}<button onclick={() => makeDefault(model)} class="btn btn-sm btn-secondary">Make default</button>{/if}<button onclick={() => openSettings(model)} aria-label={`Settings for ${model.model_id}`} class="btn btn-sm btn-icon btn-secondary"><Settings2 size={16}/></button><button onclick={() => requestModelDelete(model)} aria-label={`Delete ${model.model_id}`} class="btn btn-sm btn-icon btn-secondary text-red-500"><Trash2 size={16}/></button></div>
            </div>
          {:else}<div class="muted p-6 text-sm">No models yet. Add one manually or discover the endpoint catalogue.</div>{/each}
        </div>
      </section>
    {:else}
      <div class="surface rounded-3xl p-10 text-center"><Bot class="mx-auto text-[var(--accent)]" size={28}/><h2 class="mt-3 font-semibold">Connect your first LLM provider</h2><p class="muted mx-auto mt-2 max-w-lg text-sm">Start from a cloud, local, or OpenAI-compatible LiteLLM profile. Nothing is contacted until you test or refresh it.</p><button onclick={openNewProvider} class="btn btn-primary mt-5"><Plus size={16}/> Add provider</button></div>
    {/each}
  </div>
</div>

{#if providerModal}
  <div class="fixed inset-0 z-50 grid place-items-center bg-black/40 p-5"><div class="surface max-h-[94vh] w-full max-w-2xl overflow-y-auto rounded-3xl p-7" role="dialog" aria-modal="true" aria-labelledby="provider-title"><header class="flex items-start justify-between gap-4"><div><div class="eyebrow">LLM provider</div><h2 id="provider-title" class="mt-1 text-2xl font-semibold">{editingProvider ? `Edit ${editingProvider.label}` : 'Connect a provider'}</h2></div><button onclick={() => providerModal = false} aria-label="Close provider settings" class="btn btn-icon btn-secondary"><X size={18}/></button></header>
    <div class="mt-6 grid gap-4 sm:grid-cols-2">
      <label class="text-sm font-semibold sm:col-span-2">Starting profile<select value={providerProfileId} onchange={(event) => applyProfile(event.currentTarget.value)} class="field"><option value="">Manual configuration</option>{#each profiles as profile}<option value={profile.id}>{profile.label}</option>{/each}</select>{#if selectedProviderProfile}<small class="muted mt-1 block font-normal">{selectedProviderProfile.description}</small>{/if}</label>
      <label class="text-sm font-semibold">Display name<input bind:value={providerLabel} class="field"/></label>
      <label class="text-sm font-semibold">LiteLLM provider<input bind:value={providerKey} list="litellm-providers" class="field"/><datalist id="litellm-providers">{#each providerKeys as key}<option value={key}></option>{/each}</datalist></label>
      <label class="text-sm font-semibold sm:col-span-2">API base URL<input bind:value={providerUrl} placeholder="Provider default, or http://127.0.0.1:1234/v1" class="field"/><small class="muted mt-1 block font-normal">Leave blank when the LiteLLM adapter owns endpoint discovery.</small></label>
      <label class="text-sm font-semibold sm:col-span-2">Credential reference<input bind:value={providerSecretRef} placeholder="env:OPENAI_API_KEY" class="field"/><small class="muted mt-1 block font-normal">Use env:VARIABLE, keyring:service/user, or file:key. Secrets are never stored in this record.</small></label>
      <label class="flex items-center gap-3 rounded-xl border border-[var(--line)] p-3 text-sm font-semibold sm:col-span-2"><input type="checkbox" bind:checked={providerEnabled} class="accent-[var(--accent)]"/><span><span class="block">Provider enabled</span><small class="muted font-normal">Disabled providers remain configured but cannot be selected by new runs.</small></span></label>
      <details class="rounded-xl border border-[var(--line)] p-4 sm:col-span-2"><summary class="cursor-pointer text-sm font-semibold">Advanced LiteLLM request options</summary><p class="muted mt-2 text-xs">Add only endpoint-specific options such as organization, API version, project, location, or AWS region. Pandrator controls the model, messages, credential, timeout, temperature, and reasoning fields.</p><div class="mt-4 space-y-2">{#each requestOptionRows as row (row.id)}<div class="grid gap-2 sm:grid-cols-[minmax(9rem,.8fr)_7rem_1fr_auto]"><input bind:value={row.key} list="request-option-keys" aria-label="Request option name" placeholder="Option name" class="subfield"/><select bind:value={row.type} aria-label="Request option type" class="subfield"><option value="text">Text</option><option value="number">Number</option><option value="boolean">Boolean</option></select>{#if row.type === 'boolean'}<select bind:value={row.value} aria-label={`${row.key || 'Request option'} value`} class="subfield"><option value="true">True</option><option value="false">False</option></select>{:else}<input bind:value={row.value} type={row.type === 'number' ? 'number' : 'text'} aria-label={`${row.key || 'Request option'} value`} placeholder="Value" class="subfield"/>{/if}<button type="button" onclick={() => removeRequestOption(row.id)} aria-label="Remove request option" class="btn btn-icon btn-quiet"><Trash2 size={14}/></button></div>{/each}</div><datalist id="request-option-keys">{#each requestOptionKeys as key}<option value={key}></option>{/each}</datalist><button type="button" onclick={addRequestOption} class="btn btn-sm btn-secondary mt-3"><Plus size={13}/> Add request option</button></details>
    </div>
    <footer class="mt-6 flex justify-end gap-2"><button onclick={() => providerModal = false} class="btn btn-secondary">Cancel</button><button onclick={saveProvider} class="btn btn-primary">{editingProvider ? 'Save provider' : 'Create provider'}</button></footer>
  </div></div>
{/if}

{#if addModelProvider}<div class="fixed inset-0 z-50 grid place-items-center bg-black/35 p-5"><div class="surface w-full max-w-md rounded-3xl p-7"><h2 class="text-xl font-semibold">Add model</h2><label class="mt-5 block text-sm font-semibold">Canonical model ID<input bind:value={modelId} placeholder="Model ID exposed by the endpoint" class="field"/></label><div class="mt-5 flex justify-end gap-2"><button onclick={() => addModelProvider = null} class="btn btn-secondary">Cancel</button><button onclick={() => createModel(addModelProvider!)} class="btn btn-primary">Add model</button></div></div></div>{/if}

{#if edit}<div class="fixed inset-0 z-50 grid place-items-center bg-black/35 p-5"><div class="surface w-full max-w-xl rounded-3xl p-7"><div class="flex justify-between"><div><div class="eyebrow">Model settings</div><h2 class="mt-1 font-mono text-xl font-semibold">{edit.model_id}</h2></div><button onclick={() => edit = null} aria-label="Close model settings" class="btn btn-icon btn-secondary"><X size={18}/></button></div><div class="mt-6 grid gap-4 sm:grid-cols-2"><label class="text-sm font-semibold">Temperature<input bind:value={temperature} type="number" step="any" placeholder="Omit" class="field font-normal"/><small class="muted mt-1 block font-normal">Blank omits it; 0 is sent explicitly.</small></label><label class="text-sm font-semibold">Reasoning effort<input bind:value={reasoning} list="reasoning-values" placeholder="Omit or custom" class="field font-normal"/><datalist id="reasoning-values"><option value="minimal"></option><option value="low"></option><option value="medium"></option><option value="high"></option></datalist></label><label class="text-sm font-semibold">Input USD / million<input bind:value={inputCost} type="number" min="0" step="any" class="field font-normal"/></label><label class="text-sm font-semibold">Cached input USD / million<input bind:value={cachedCost} type="number" min="0" step="any" placeholder="Use input rate" class="field font-normal"/></label><label class="text-sm font-semibold sm:col-span-2">Output USD / million<input bind:value={outputCost} type="number" min="0" step="any" class="field font-normal"/></label></div><button onclick={saveModel} class="btn btn-primary mt-6 w-full">Save model defaults</button></div></div>{/if}

{#if deletingModel}<div class="fixed inset-0 z-50 grid place-items-center bg-black/35 p-5"><div class="surface w-full max-w-md rounded-3xl p-7"><h2 class="text-xl font-semibold">Remove {deletingModel.model_id}?</h2>{#if deletingModel.is_default}<label class="mt-5 block text-sm font-semibold">Replacement application default<select bind:value={replacementModelRecordId} class="field"><option value="">Choose a replacement</option>{#each allModels.filter((item) => item.id !== deletingModel?.id) as model}<option value={model.id}>{providers.find((item) => item.id === model.provider_id)?.label} · {model.model_id}</option>{/each}</select></label>{:else}<p class="muted mt-3 text-sm">Its settings are removed; completed run snapshots remain unchanged.</p>{/if}<div class="mt-6 flex justify-end gap-2"><button onclick={() => deletingModel = null} class="btn btn-secondary">Cancel</button><button onclick={removeModel} disabled={deletingModel.is_default && !replacementModelRecordId} class="btn btn-primary">Remove model</button></div></div></div>{/if}

{#if deletingProvider}<div class="fixed inset-0 z-50 grid place-items-center bg-black/35 p-5"><div class="surface w-full max-w-md rounded-3xl p-7"><h2 class="text-xl font-semibold">Remove {deletingProvider.label}?</h2><p class="muted mt-3 text-sm">The provider and its model records will be removed. Completed run snapshots remain unchanged.</p>{#if (models[deletingProvider.id] ?? []).some((item) => item.is_default)}<label class="mt-5 block text-sm font-semibold">Replacement application default<select bind:value={replacementModelRecordId} class="field"><option value="">Choose a replacement</option>{#each allModels.filter((item) => item.provider_id !== deletingProvider?.id) as model}<option value={model.id}>{providers.find((item) => item.id === model.provider_id)?.label} · {model.model_id}</option>{/each}</select></label>{/if}<div class="mt-6 flex justify-end gap-2"><button onclick={() => deletingProvider = null} class="btn btn-secondary">Cancel</button><button onclick={removeProvider} disabled={(models[deletingProvider.id] ?? []).some((item) => item.is_default) && !replacementModelRecordId} class="btn btn-primary">Remove provider</button></div></div></div>{/if}

<GuidedTour tourId="models" steps={tourSteps} bind:open={tourOpen}/>

<style>
  .field{margin-top:.4rem;width:100%;border:1px solid var(--line);border-radius:.75rem;background:var(--paper);padding:.68rem .78rem;font-weight:400}
  .subfield{width:100%;min-height:2.35rem;border:1px solid var(--line);border-radius:.65rem;background:var(--paper);padding:.5rem .65rem;font-size:.75rem;font-weight:400;color:var(--ink)}
</style>
