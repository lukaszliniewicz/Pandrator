<script lang="ts">
  import { CheckCircle2, Plus, RefreshCw, Server, X } from '@lucide/svelte';
  import { api } from './api';

  let payload = $state<any>({ services: [], profiles: [], value: {}, revision: 0, default_value: {}, default_service: 'XTTS', default_revision: 0 });
  let discoverOpen = $state(false);
  let baseUrl = $state('http://127.0.0.1:8000');
  let discovering = $state(false);
  let refreshing = $state('');
  let result = $state<any>(null);
  let error = $state('');

  async function load() { payload = await api('/services/tts'); }

  async function discover() {
    discovering = true;
    error = '';
    try { result = await api('/services/tts/discover', { method: 'POST', body: JSON.stringify({ base_url: baseUrl }) }); }
    catch (caught) { error = caught instanceof Error ? caught.message : String(caught); }
    finally { discovering = false; }
  }

  async function persist(value: any) {
    await api('/settings/services.tts', { method: 'PUT', headers: { 'If-Match': `"${payload.revision}"` }, body: JSON.stringify({ value }) });
    await load();
  }

  function recordFrom(candidate: any, existing: any = {}) {
    return { ...existing, id: existing.id ?? candidate.id ?? String(candidate.name ?? 'service').toLowerCase().replace(/[^a-z0-9]+/g, '_'), name: candidate.name, kind: candidate.kind, api_base: candidate.api_base, provider: candidate.provider, adapter: candidate.adapter, speech_path: candidate.speech_path, models_path: candidate.models_path, voices_path: candidate.voices_path, request_fields: candidate.request_fields, request_defaults: candidate.request_defaults, models: candidate.models ?? [], voices: candidate.voices ?? [], default_model: candidate.default_model, default_voice: candidate.default_voice, supports_prebuilt_voices: candidate.supports_prebuilt_voices };
  }

  async function saveDiscovered() {
    if (!result?.success) return;
    const existing = (payload.value.provider_configs ?? []).find((item: any) => item.api_base === result.api_base);
    await persist({ ...payload.value, provider_configs: [...(payload.value.provider_configs ?? []).filter((item: any) => item.api_base !== result.api_base), recordFrom(result, existing)] });
    discoverOpen = false;
    result = null;
  }

  async function useProfile(profile: any) {
    const existing = (payload.value.provider_configs ?? []).find((item: any) => item.id === profile.id || item.api_base === profile.api_base);
    await persist({ ...payload.value, provider_configs: [...(payload.value.provider_configs ?? []).filter((item: any) => item.id !== profile.id && item.api_base !== profile.api_base), recordFrom(profile, existing ?? { id: profile.id })] });
  }

  async function setDefault(service: any) {
    const { provider_configs: _legacyProviders, ...defaults } = payload.default_value ?? {};
    await api('/settings/defaults.tts', { method: 'PUT', headers: { 'If-Match': `"${payload.default_revision}"` }, body: JSON.stringify({ value: { ...defaults, service: service.id } }) });
    await load();
  }

  const isDefault = (service: any) => [service.id, service.name].map((value) => String(value ?? '').toLowerCase()).includes(String(payload.default_service ?? '').toLowerCase());

  async function removeService(service: any) {
    await persist({ ...payload.value, provider_configs: (payload.value.provider_configs ?? []).filter((item: any) => item.id !== service.id && item.api_base !== service.api_base) });
  }

  async function refreshService(service: any) {
    refreshing = service.id;
    error = '';
    try {
      const found = await api<any>('/services/tts/discover', { method: 'POST', body: JSON.stringify({ base_url: service.api_base }) });
      const existing = (payload.value.provider_configs ?? []).find((item: any) => item.id === service.id || item.api_base === service.api_base) ?? { id: service.id };
      await persist({ ...payload.value, provider_configs: [...(payload.value.provider_configs ?? []).filter((item: any) => item.id !== existing.id && item.api_base !== service.api_base), recordFrom(found, existing)] });
    } catch (caught) { error = caught instanceof Error ? caught.message : String(caught); }
    finally { refreshing = ''; }
  }

  load();
</script>

<section>
  <div class="flex flex-wrap items-end justify-between gap-4"><div><div class="eyebrow">TTS services</div><h2 class="mt-1 text-2xl font-semibold">Speech connections</h2><p class="muted mt-2 max-w-2xl text-sm">Endpoints and catalogues live here. Session pages select among them and keep generation behavior separate.</p></div><div class="flex gap-2"><button onclick={load} class="btn btn-secondary"><RefreshCw size={16}/> Refresh all</button><button onclick={() => discoverOpen = true} class="btn btn-primary"><Plus size={16}/> Connect endpoint</button></div></div>
  {#if error}<p class="mt-3 text-sm text-red-500">{error}</p>{/if}
  <div class="mt-5 grid gap-3 md:grid-cols-2">
    {#each payload.services ?? [] as service}
      <article class="rounded-2xl border border-[var(--line)] p-4"><div class="flex items-center gap-3"><div class="grid size-9 place-items-center rounded-xl bg-[var(--accent-soft)] text-[var(--accent)]"><Server size={17}/></div><div class="min-w-0 flex-1"><div class="truncate font-semibold">{service.name}</div><div class="muted truncate text-xs">{service.api_base}</div></div>{#if service.models?.length}<CheckCircle2 class="text-[var(--success)]" size={17}/>{/if}</div><div class="muted mt-3 text-xs">{service.models?.length ?? 0} models · {service.voices?.length ?? 0} voices</div><div class="mt-3 flex flex-wrap gap-2"><button class="btn btn-sm btn-secondary" onclick={() => setDefault(service)} disabled={isDefault(service)}>{isDefault(service) ? 'Default' : 'Make default'}</button><button class="btn btn-sm btn-secondary" onclick={() => refreshService(service)} disabled={refreshing === service.id}><RefreshCw size={13}/>{refreshing === service.id ? 'Testing…' : 'Test & refresh'}</button>{#if (payload.value.provider_configs ?? []).some((item: any) => item.id === service.id || item.api_base === service.api_base)}<button class="btn btn-sm btn-secondary text-red-500" onclick={() => removeService(service)}>Remove</button>{/if}</div></article>
    {/each}
  </div>
  <details class="mt-6 rounded-2xl border border-[var(--line)] p-4"><summary class="cursor-pointer font-semibold">Known compatible endpoint profiles</summary><div class="mt-4 grid gap-2 md:grid-cols-2">{#each payload.profiles ?? [] as profile}<div class="rounded-xl border border-[var(--line)] p-3"><div class="text-sm font-semibold">{profile.name}</div><div class="muted mt-1 text-xs">{profile.description}</div><button onclick={() => useProfile(profile)} class="btn btn-sm btn-secondary mt-3">Use profile</button></div>{/each}</div></details>
</section>

{#if discoverOpen}
  <div class="fixed inset-0 z-50 grid place-items-center bg-black/40 p-5"><div class="surface w-full max-w-xl rounded-3xl p-7"><div class="flex justify-between"><div><div class="eyebrow">Connect TTS</div><h2 class="mt-1 text-2xl font-semibold">Discover an endpoint</h2></div><button onclick={() => discoverOpen = false} aria-label="Close endpoint discovery" class="btn btn-icon btn-secondary"><X size={19}/></button></div><label class="mt-6 block text-sm font-semibold">Base URL<input bind:value={baseUrl} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3"/></label><button onclick={discover} disabled={discovering} class="btn btn-primary mt-4 w-full">{discovering ? 'Inspecting…' : 'Detect API'}</button>{#if error}<p class="mt-3 text-sm text-red-500">{error}</p>{/if}{#if result}<div class="mt-5 rounded-xl border border-[var(--line)] p-4"><div class="font-semibold">{result.name}</div><p class="muted mt-2 text-sm">{result.message}</p><div class="muted mt-2 text-xs">{result.models?.length ?? 0} models · {result.voices?.length ?? 0} voices · {result.confidence} confidence</div><button onclick={saveDiscovered} class="btn btn-primary mt-4">Save connection</button></div>{/if}</div></div>
{/if}
