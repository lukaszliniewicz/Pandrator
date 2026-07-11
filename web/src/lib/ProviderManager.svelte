<script lang="ts">
  import { ArrowLeft, Bot, Plus, RefreshCw, Settings2, Trash2, X } from '@lucide/svelte';
  import { api } from './api';
  import { onMount } from 'svelte';
  import GuidedTour from './GuidedTour.svelte';

  type Provider = { id: string; provider_key: string; label: string; base_url?: string; secret_ref?: string; enabled: boolean };
  type Model = { id: string; provider_id: string; model_id: string; is_default: boolean; default_temperature: number | null; default_reasoning_effort: string | null; input_cost_per_million: number | null; cached_input_cost_per_million: number | null; output_cost_per_million: number | null; revision: number };
  let { onback }: { onback: () => void } = $props();
  let providers = $state<Provider[]>([]);
  let models = $state<Record<string, Model[]>>({});
  let error = $state('');
  let edit = $state<Model | null>(null);
  let addProvider = $state(false);
  let providerLabel = $state(''); let providerKey = $state('openai'); let providerUrl = $state(''); let providerSecretRef = $state('env:OPENAI_API_KEY');
  let modelId = $state(''); let addModelProvider = $state<string | null>(null);
  let temperature = $state(''); let reasoning = $state(''); let inputCost = $state(''); let cachedCost = $state(''); let outputCost = $state('');
  let tourOpen = $state(false);
  const tourSteps = [{section:'Models',title:'One record per canonical model',body:'Add discovered or manual model IDs under their provider. Refresh preserves manual records and their settings.'},{section:'Defaults',title:'Request parameters are optional',body:'A blank temperature or reasoning value is omitted. Zero is a specific temperature and is sent.'},{section:'Costs',title:'Provider cost wins',body:'Custom uncached, cached, and output rates are used only when the provider does not return an authoritative cost.'}];

  async function load() {
    try {
      const result = await api<{items: Provider[]}>('/providers'); providers = result.items;
      const entries = await Promise.all(providers.map(async (provider) => [provider.id, (await api<{items: Model[]}>(`/providers/${provider.id}/models`)).items] as const));
      models = Object.fromEntries(entries);
    } catch (caught) { error = caught instanceof Error ? caught.message : String(caught); }
  }
  function openSettings(model: Model) {
    edit = model; temperature = model.default_temperature?.toString() ?? ''; reasoning = model.default_reasoning_effort ?? '';
    inputCost = model.input_cost_per_million?.toString() ?? ''; cachedCost = model.cached_input_cost_per_million?.toString() ?? ''; outputCost = model.output_cost_per_million?.toString() ?? '';
  }
  const optionalNumber = (value: string) => value.trim() === '' ? null : Number(value);
  async function saveModel() {
    if (!edit) return;
    try {
      await api(`/providers/${edit.provider_id}/models/${edit.id}`, { method: 'PATCH', headers: {'If-Match': `"${edit.revision}"`}, body: JSON.stringify({ default_temperature: optionalNumber(temperature), default_reasoning_effort: reasoning.trim() || null, input_cost_per_million: optionalNumber(inputCost), cached_input_cost_per_million: optionalNumber(cachedCost), output_cost_per_million: optionalNumber(outputCost) }) });
      edit = null; await load();
    } catch (caught) { error = caught instanceof Error ? caught.message : String(caught); }
  }
  async function makeDefault(model: Model) {
    try { await api(`/providers/${model.provider_id}/models/${model.id}`, { method:'PATCH', headers:{'If-Match':`"${model.revision}"`}, body:JSON.stringify({is_default:true}) }); await load(); }
    catch (caught) { error = caught instanceof Error ? caught.message : String(caught); }
  }
  async function createProvider() {
    try { await api('/providers', {method:'POST', body:JSON.stringify({provider_key:providerKey, label:providerLabel, base_url:providerUrl || null, secret_ref:providerSecretRef || null})}); addProvider=false; await load(); }
    catch (caught) { error = caught instanceof Error ? caught.message : String(caught); }
  }
  async function createModel(providerId: string) {
    if (!modelId.trim()) return;
    try { await api(`/providers/${providerId}/models`, {method:'POST', body:JSON.stringify({model_id:modelId.trim(), is_default:!(models[providerId]?.length)})}); modelId=''; addModelProvider=null; await load(); }
    catch (caught) { error = caught instanceof Error ? caught.message : String(caught); }
  }
  async function remove(model: Model) {
    const replacement = models[model.provider_id]?.find((item) => item.id !== model.id)?.model_id;
    try { await api(`/providers/${model.provider_id}/models/${model.id}`, {method:'DELETE', body:JSON.stringify({replacement_model_id:replacement ?? null})}); await load(); }
    catch (caught) { error = caught instanceof Error ? caught.message : String(caught); }
  }
  async function refresh(providerId: string) {
    try { await api(`/providers/${providerId}/models/refresh`, {method:'POST', body:'{}'}); await load(); }
    catch (caught) { error = caught instanceof Error ? caught.message : String(caught); }
  }
  async function testProvider(providerId:string){const selected=models[providerId]?.find((item)=>item.is_default)??models[providerId]?.[0];if(!selected){error='Add a model before testing this provider.';return;}try{const result=await api<{model:string}>(`/providers/${providerId}/test`,{method:'POST',body:JSON.stringify({model_id:selected.model_id})});error='';window.alert(`Provider test succeeded with ${result.model}.`);}catch(caught){error=caught instanceof Error?caught.message:String(caught)}}
  onMount(load);
</script>

<div class="mx-auto max-w-6xl">
  <button onclick={onback} class="muted mb-7 flex items-center gap-2 text-sm font-semibold"><ArrowLeft size={17}/> Workspace</button>
  <header class="mb-8 flex flex-wrap items-end justify-between gap-5"><div><div class="eyebrow">Providers</div><h1 class="mt-2 text-4xl font-semibold">LLM models</h1><p class="muted mt-3 max-w-2xl">Defaults live on each canonical model. Blank temperature or reasoning values are omitted from requests.</p></div><div class="flex gap-2"><button onclick={() => tourOpen=true} class="rounded-xl border border-[var(--line)] px-4 py-2.5 text-sm font-semibold">Tour</button><button onclick={() => providers[0] && testProvider(providers[0].id)} disabled={!providers.length} class="rounded-xl border border-[var(--line)] px-4 py-2.5 text-sm font-semibold disabled:opacity-40">Test default</button><button onclick={() => addProvider=true} class="flex items-center gap-2 rounded-xl bg-[var(--accent)] px-4 py-2.5 text-sm font-semibold text-white"><Plus size={17}/> Add provider</button></div></header>
  {#if error}<div class="mb-5 rounded-xl border border-red-400/40 bg-red-500/10 px-4 py-3 text-sm">{error}</div>{/if}
  <div class="space-y-6">{#each providers as provider}<section class="surface overflow-hidden rounded-[1.5rem]"><header class="flex flex-wrap items-center justify-between gap-4 border-b border-[var(--line)] p-5"><div class="flex items-center gap-3"><div class="grid size-10 place-items-center rounded-xl bg-[var(--accent-soft)] text-[var(--accent)]"><Bot size={19}/></div><div><h2 class="font-semibold">{provider.label}</h2><p class="muted text-xs">{provider.provider_key}{provider.base_url ? ` · ${provider.base_url}` : ''}</p></div></div><div class="flex gap-2"><button onclick={() => refresh(provider.id)} class="flex items-center gap-2 rounded-lg border border-[var(--line)] px-3 py-2 text-xs font-semibold"><RefreshCw size={14}/> Refresh</button><button onclick={() => { addModelProvider=provider.id; modelId=''; }} class="flex items-center gap-2 rounded-lg border border-[var(--line)] px-3 py-2 text-xs font-semibold"><Plus size={14}/> Add model</button></div></header>
    <div>{#each models[provider.id] ?? [] as model}<div class="flex flex-wrap items-center gap-4 border-b border-[var(--line)] px-5 py-4 last:border-0"><div class="min-w-0 flex-1"><div class="flex items-center gap-2"><span class="truncate font-mono text-sm font-semibold">{model.model_id}</span>{#if model.is_default}<span class="rounded-full bg-[var(--accent-soft)] px-2 py-0.5 text-[.65rem] font-bold uppercase text-[var(--accent)]">Default</span>{/if}</div><p class="muted mt-1 text-xs">Temperature {model.default_temperature ?? 'omit'} · Reasoning {model.default_reasoning_effort || 'omit'} · Input ${model.input_cost_per_million ?? '—'} / cached ${model.cached_input_cost_per_million ?? model.input_cost_per_million ?? '—'} / output ${model.output_cost_per_million ?? '—'}</p></div><div class="flex gap-2">{#if !model.is_default}<button onclick={() => makeDefault(model)} class="rounded-lg border border-[var(--line)] px-3 py-2 text-xs font-semibold">Make default</button>{/if}<button onclick={() => openSettings(model)} aria-label={`Settings for ${model.model_id}`} class="rounded-lg border border-[var(--line)] p-2"><Settings2 size={16}/></button><button onclick={() => remove(model)} aria-label={`Delete ${model.model_id}`} class="rounded-lg border border-[var(--line)] p-2 text-red-500"><Trash2 size={16}/></button></div></div>{:else}<div class="muted p-6 text-sm">No models yet. Add one manually or refresh discovery.</div>{/each}</div>
  </section>{:else}<div class="surface rounded-3xl p-10 text-center"><Bot class="mx-auto text-[var(--accent)]" size={28}/><h2 class="mt-3 font-semibold">Add your first provider</h2></div>{/each}</div>
</div>

{#if addModelProvider}<div class="fixed inset-0 z-50 grid place-items-center bg-black/35 p-5"><div class="surface w-full max-w-md rounded-3xl p-7"><h2 class="text-xl font-semibold">Add model</h2><input bind:value={modelId} placeholder="Canonical model ID" class="mt-5 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3"/><div class="mt-5 flex justify-end gap-2"><button onclick={() => addModelProvider=null} class="rounded-xl border border-[var(--line)] px-4 py-2">Cancel</button><button onclick={() => createModel(addModelProvider!)} class="rounded-xl bg-[var(--accent)] px-4 py-2 text-white">Add</button></div></div></div>{/if}
{#if addProvider}<div class="fixed inset-0 z-50 grid place-items-center bg-black/35 p-5"><div class="surface w-full max-w-lg rounded-3xl p-7"><div class="flex justify-between"><h2 class="text-xl font-semibold">Add provider</h2><button onclick={() => addProvider=false}><X size={18}/></button></div><div class="mt-5 grid gap-4"><label class="text-sm font-semibold">Display name<input bind:value={providerLabel} class="mt-1 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2"/></label><label class="text-sm font-semibold">Provider key<input bind:value={providerKey} class="mt-1 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2"/></label><label class="text-sm font-semibold">Base URL<input bind:value={providerUrl} class="mt-1 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2"/></label><label class="text-sm font-semibold">Secret reference<input bind:value={providerSecretRef} placeholder="env:OPENAI_API_KEY" class="mt-1 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2"/></label></div><button onclick={createProvider} class="mt-6 w-full rounded-xl bg-[var(--accent)] px-4 py-2.5 font-semibold text-white">Create provider</button></div></div>{/if}
{#if edit}<div class="fixed inset-0 z-50 grid place-items-center bg-black/35 p-5"><div class="surface w-full max-w-xl rounded-3xl p-7"><div class="flex justify-between"><div><div class="eyebrow">Model settings</div><h2 class="mt-1 font-mono text-xl font-semibold">{edit.model_id}</h2></div><button onclick={() => edit=null}><X size={18}/></button></div><div class="mt-6 grid gap-4 sm:grid-cols-2"><label class="text-sm font-semibold">Temperature<input bind:value={temperature} type="number" step="any" placeholder="Omit" class="mt-1 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"/></label><label class="text-sm font-semibold">Reasoning effort<input bind:value={reasoning} list="reasoning-values" placeholder="Omit or custom" class="mt-1 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"/><datalist id="reasoning-values"><option value="minimal"></option><option value="low"></option><option value="medium"></option><option value="high"></option></datalist></label><label class="text-sm font-semibold">Input USD / million<input bind:value={inputCost} type="number" min="0" step="any" class="mt-1 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"/></label><label class="text-sm font-semibold">Cached input USD / million<input bind:value={cachedCost} type="number" min="0" step="any" placeholder="Use input rate" class="mt-1 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"/></label><label class="text-sm font-semibold sm:col-span-2">Output USD / million<input bind:value={outputCost} type="number" min="0" step="any" class="mt-1 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"/></label></div><button onclick={saveModel} class="mt-6 w-full rounded-xl bg-[var(--accent)] px-4 py-2.5 font-semibold text-white">Save model defaults</button></div></div>{/if}
<GuidedTour tourId="models" steps={tourSteps} bind:open={tourOpen}/>
