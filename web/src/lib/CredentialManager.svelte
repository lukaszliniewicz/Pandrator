<script lang="ts">
  import { CheckCircle2, KeyRound, Trash2 } from '@lucide/svelte';
  import { api } from './api';

  type CredentialProfile = {
    id: string;
    label: string;
    description: string;
    environment_variable: string;
    credential_configured: boolean;
    credential_source: string;
  };

  let items = $state<CredentialProfile[]>([]);
  let values = $state<Record<string, string>>({});
  let busy = $state('');
  let error = $state('');
  let notice = $state('');

  async function load() {
    try {
      items = (await api<{ items: CredentialProfile[] }>('/credentials')).items;
      error = '';
    } catch (caught) {
      error = caught instanceof Error ? caught.message : String(caught);
    }
  }

  async function save(item: CredentialProfile) {
    const apiKey = String(values[item.id] ?? '').trim();
    if (!apiKey) { error = `Enter a ${item.label} API key.`; return; }
    busy = item.id; error = ''; notice = '';
    try {
      await api(`/credentials/${item.id}`, { method: 'PUT', body: JSON.stringify({ api_key: apiKey }) });
      values = { ...values, [item.id]: '' };
      notice = `${item.label} API key saved.`;
      await load();
    } catch (caught) {
      error = caught instanceof Error ? caught.message : String(caught);
    } finally { busy = ''; }
  }

  async function remove(item: CredentialProfile) {
    busy = item.id; error = ''; notice = '';
    try {
      await api(`/credentials/${item.id}`, { method: 'PUT', body: JSON.stringify({ clear: true }) });
      values = { ...values, [item.id]: '' };
      notice = `${item.label} database key removed.`;
      await load();
    } catch (caught) {
      error = caught instanceof Error ? caught.message : String(caught);
    } finally { busy = ''; }
  }

  load();
</script>

<section>
  <header><div class="eyebrow">Other API keys</div><h2 class="mt-1 text-2xl font-semibold">Supporting services</h2><p class="muted mt-2 max-w-2xl text-sm">Keys used outside the LLM and speech provider catalogues are managed here with the same write-only behavior.</p></header>
  {#if error}<p role="alert" class="mt-4 rounded-xl border border-red-400/40 bg-red-500/10 p-3 text-sm text-red-600">{error}</p>{/if}
  {#if notice}<p role="status" class="mt-4 flex items-center gap-2 rounded-xl border border-[var(--line)] bg-[var(--accent-soft)] p-3 text-sm"><CheckCircle2 size={16}/>{notice}</p>{/if}
  <div class="mt-5 grid gap-4 lg:grid-cols-2">
    {#each items as item}
      <article class="rounded-2xl border border-[var(--line)] p-5">
        <div class="flex items-start gap-3"><div class="grid size-10 shrink-0 place-items-center rounded-xl bg-[var(--accent-soft)] text-[var(--accent)]"><KeyRound size={18}/></div><div><div class="flex flex-wrap items-center gap-2"><h3 class="font-semibold">{item.label}</h3><span class="rounded-full border border-[var(--line)] px-2 py-0.5 text-[.62rem] font-bold uppercase">{item.credential_configured ? `Key: ${item.credential_source}` : 'No key'}</span></div><p class="muted mt-1 text-xs">{item.description}</p></div></div>
        <label class="mt-5 block text-sm font-semibold">API key<input value={values[item.id] ?? ''} oninput={(event) => values = { ...values, [item.id]: event.currentTarget.value }} type="password" autocomplete="new-password" placeholder={item.credential_configured ? 'Leave blank to keep the current key' : 'Paste API key'} class="field"/><small class="muted mt-1 block font-normal">Saved in Pandrator's local database and never returned. Environment fallback: {item.environment_variable}.</small></label>
        <div class="mt-4 flex flex-wrap gap-2"><button onclick={() => save(item)} disabled={busy === item.id || !String(values[item.id] ?? '').trim()} class="btn btn-primary">{item.credential_configured ? 'Replace key' : 'Save key'}</button>{#if item.credential_configured && item.credential_source === 'database'}<button onclick={() => remove(item)} disabled={busy === item.id} class="btn btn-secondary text-red-500"><Trash2 size={14}/> Remove database key</button>{/if}</div>
      </article>
    {/each}
  </div>
</section>

<style>
  .field{margin-top:.4rem;width:100%;border:1px solid var(--line);border-radius:.75rem;background:var(--paper);padding:.68rem .78rem;font-weight:400;color:var(--ink)}
</style>
