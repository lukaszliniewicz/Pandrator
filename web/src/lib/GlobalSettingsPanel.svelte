<script lang="ts">
  import { ExternalLink, RotateCcw, Save } from '@lucide/svelte';
  import { api } from './api';
  import SettingField from './SettingField.svelte';
  import { GLOBAL_TTS_KEYS } from './settings-fields';

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
    return GLOBAL_TTS_KEYS.has(key);
  }).sort(([a], [b]) => a.localeCompare(b)));

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

  async function restoreBuiltins() {
    saving = true; message = '';
    try {
      const result = await api<any>(`/settings/defaults.${section}`, { method: 'PUT', headers: { 'If-Match': `"${payload.revision}"` }, body: JSON.stringify({ value: {} }) });
      payload = { ...payload, revision: result.revision, value: {}, effective: { ...payload.builtin } };
      value = {};
      message = 'Restored built-in defaults.';
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
        <SettingField {section} keyName={key} value={current(key, fallback)} onchange={(next) => set(key, next)} compact/>
      {/each}
    </div>
    <div class="mt-4 flex flex-wrap items-center gap-3"><button onclick={restoreBuiltins} disabled={saving || !Object.keys(value).length} class="btn btn-sm btn-secondary"><RotateCcw size={13}/> Restore built-in defaults</button><button onclick={save} disabled={saving} class="btn btn-sm btn-primary"><Save size={13}/>{saving ? 'Saving…' : 'Save global defaults'}</button>{#if message}<span class="muted text-xs">{message}</span>{/if}</div>
  {/if}
</div>
