<script lang="ts">
  import { ExternalLink, Save } from '@lucide/svelte';
  import { api } from './api';

  let { section }: { section: string } = $props();
  let payload = $state<any>(null);
  let value = $state<Record<string, any>>({});
  let saving = $state(false);
  let message = $state('');

  const providerSetting = (key: string) => key === 'provider_configs' || key === 'use_external_server' || key === 'external_server_url' || key === 'openai_audio_endpoint' || key.endsWith('_base_url') || key.endsWith('_api_key');
  const entries = $derived(Object.entries(payload?.effective ?? {}).filter(([key]) => {
    if (section === 'output' && key === 'cover_artifact_id') return false;
    if (section !== 'tts') return true;
    if (providerSetting(key)) return false;
    const service = String(value.service ?? payload?.effective?.service ?? '').toLowerCase();
    if (key.startsWith('voxcpm_')) return service.includes('voxcpm');
    if (key.startsWith('fishs2_')) return service.includes('fish');
    if (key.startsWith('voxtral_')) return service.includes('voxtral');
    if (key.startsWith('chatterbox_')) return service.includes('chatterbox');
    if (key.startsWith('xtts_')) return service.includes('xtts');
    if (key.startsWith('openai_audio_')) return service.includes('openai') || service.includes('gemini') || service.includes('custom');
    return true;
  }).sort(([a], [b]) => a.localeCompare(b)));

  const label = (key: string) => key.replaceAll('_', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
  const current = (key: string, fallback: any) => Object.prototype.hasOwnProperty.call(value, key) ? value[key] : fallback;
  const set = (key: string, next: any) => value = { ...value, [key]: next };

  async function load() {
    payload = await api(`/defaults/${section}`);
    value = { ...(payload.value ?? {}) };
  }

  async function save() {
    saving = true;
    message = '';
    try {
      if (section === 'tts') {
        value = Object.fromEntries(Object.entries(value).filter(([key]) => !providerSetting(key)));
      }
      const result = await api<any>(`/settings/defaults.${section}`, { method: 'PUT', headers: { 'If-Match': `"${payload.revision}"` }, body: JSON.stringify({ value }) });
      payload = { ...payload, revision: result.revision, value: result.value, effective: { ...payload.builtin, ...result.value } };
      message = 'Saved.';
    } catch (caught) { message = caught instanceof Error ? caught.message : String(caught); }
    finally { saving = false; }
  }

  load();
</script>

<div class="mt-4 border-t border-[var(--line)] pt-4">
  {#if section === 'tts'}
    <div class="mb-4 flex flex-wrap items-center justify-between gap-3 rounded-xl bg-[var(--accent-soft)] p-3"><p class="text-xs">These are generation defaults. Endpoint URLs, credentials, and provider catalogues are managed separately.</p><a href="/providers?tab=tts" class="btn btn-sm btn-secondary"><ExternalLink size={14}/> Manage TTS services</a></div>
  {/if}
  {#if payload}
    <div class="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
      {#each entries as [key, fallback]}
        <label class="text-xs font-semibold">{label(key)}
          {#if typeof fallback === 'boolean'}
            <span class="field flex items-center gap-2"><input type="checkbox" checked={current(key, fallback)} onchange={(event) => set(key, (event.currentTarget as HTMLInputElement).checked)}/>{current(key, fallback) ? 'Enabled' : 'Disabled'}</span>
          {:else if typeof fallback === 'number'}
            <input class="field" type="number" step="any" value={current(key, fallback)} oninput={(event) => set(key, Number((event.currentTarget as HTMLInputElement).value))}/>
          {:else if typeof fallback === 'object'}
            <textarea class="field font-mono text-xs" rows="2" value={JSON.stringify(current(key, fallback), null, 2)} onblur={(event) => { try { set(key, JSON.parse((event.currentTarget as HTMLTextAreaElement).value)); } catch { message = 'Invalid JSON.'; } }}></textarea>
          {:else}
            <input class="field" value={current(key, fallback) ?? ''} oninput={(event) => set(key, (event.currentTarget as HTMLInputElement).value)}/>
          {/if}
        </label>
      {/each}
    </div>
    <div class="mt-4 flex items-center gap-3"><button onclick={save} disabled={saving} class="btn btn-sm btn-primary"><Save size={13}/>{saving ? 'Saving…' : 'Save global defaults'}</button>{#if message}<span class="muted text-xs">{message}</span>{/if}</div>
  {/if}
</div>

<style>.field{margin-top:.35rem;width:100%;border:1px solid var(--line);border-radius:.65rem;background:var(--paper);padding:.55rem;font-weight:400}</style>
