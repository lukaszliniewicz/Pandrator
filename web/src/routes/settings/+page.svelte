<script lang="ts">
  import { RefreshCw, Save, ShieldCheck } from '@lucide/svelte';
  import { api } from '$lib/api';
  import { appState } from '$lib/app-state.svelte';
  import GlobalSettingsPanel from '$lib/GlobalSettingsPanel.svelte';

  let wizardVisible = $state(false);
  let retention = $state(30);
  let revision = $state(0);
  let saving = $state(false);
  let message = $state('');
  const sections = ['text', 'stt', 'subtitles', 'correction', 'translation', 'tts', 'audio', 'rvc', 'source_cleaning', 'output'];
  const acronyms: Record<string, string> = { tts: 'TTS', stt: 'STT', rvc: 'RVC' };
  const sectionLabel = (section: string) => acronyms[section] ?? section.replaceAll('_', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());

  async function load() {
    try {
      const result = await api<any>('/settings/web.preferences');
      revision = result.revision;
      wizardVisible = Boolean(result.value?.show_startup_wizard);
      retention = Number(result.value?.retention_days ?? 30);
    } catch { revision = 0; }
  }

  async function save() {
    saving = true;
    message = '';
    try {
      const result = await api<any>('/settings/web.preferences', { method: 'PUT', headers: { 'If-Match': `"${revision}"` }, body: JSON.stringify({ value: { show_startup_wizard: wizardVisible, retention_days: retention } }) });
      revision = result.revision;
      message = 'Application preferences saved.';
    } catch (caught) { message = caught instanceof Error ? caught.message : String(caught); }
    finally { saving = false; }
  }

  load();
</script>

<div class="mx-auto max-w-5xl">
  <header class="flex items-end justify-between gap-4"><div><div class="eyebrow">Application settings</div><h1 class="mt-2 text-4xl font-semibold">Pandrator</h1><p class="muted mt-3">Global behavior, readiness, privacy, storage, and maintenance.</p></div><button onclick={save} disabled={saving} class="btn btn-primary"><Save size={16}/>{saving ? 'Saving…' : 'Save settings'}</button></header>
  {#if message}<p class="mt-4 rounded-xl bg-[var(--accent-soft)] p-3 text-sm">{message}</p>{/if}
  <div class="mt-8 grid gap-5 md:grid-cols-2">
    <section class="surface rounded-2xl p-6"><div class="eyebrow">Onboarding</div><h2 class="mt-2 text-xl font-semibold">Setup and tours</h2><label class="mt-5 flex items-start gap-3"><input type="checkbox" bind:checked={wizardVisible} class="mt-1 accent-[var(--accent)]"/><span><strong class="block text-sm">Show guided creation on Home</strong><small class="muted">The launcher can always be opened manually from Home.</small></span></label></section>
    <section class="surface rounded-2xl p-6"><div class="eyebrow">Runtime</div><h2 class="mt-2 text-xl font-semibold">Capability snapshot</h2><div class="muted mt-4 space-y-2 text-sm"><div>FFmpeg: {appState.capabilities?.ffmpeg?.available ? 'Ready' : 'Missing'}</div><div>CrispASR: {appState.capabilities?.stt?.crispasr ? 'Ready' : 'Missing'}</div><div>GPU: {appState.capabilities?.gpu?.available ? 'Detected' : 'Not detected'}</div></div><button onclick={() => appState.refreshCapabilities()} class="btn btn-secondary mt-5"><RefreshCw size={15}/> Probe again</button></section>
    <section class="surface rounded-2xl p-6"><div class="eyebrow">Retention</div><h2 class="mt-2 text-xl font-semibold">Temporary data</h2><label class="mt-5 block text-sm font-semibold">Keep compactable logs and temporary history for<input type="number" min="1" max="3650" bind:value={retention} class="mx-2 w-20 rounded-lg border border-[var(--line)] bg-[var(--paper)] px-2 py-1"/>days</label><p class="muted mt-3 text-xs">User-created sources and artifacts are never deleted by retention policy.</p></section>
    <section class="surface rounded-2xl p-6"><div class="flex items-center gap-3"><ShieldCheck class="text-[var(--accent)]"/><div><div class="eyebrow">Privacy</div><h2 class="mt-1 text-xl font-semibold">Single-owner workspace</h2></div></div><p class="muted mt-4 text-sm leading-relaxed">Credentials are resolved from secret references, environment variables, or the OS keyring. Run snapshots never store plaintext secrets.</p></section>
  </div>
  <section class="surface mt-6 rounded-2xl p-6"><div class="eyebrow">Defaults</div><h2 class="mt-2 text-xl font-semibold">Defaults for new and existing sessions</h2><p class="muted mt-2 text-sm">Session overrides take precedence. Provider and endpoint connections live under Providers & services.</p><div class="mt-5 space-y-2">{#each sections as section}<details class="rounded-xl border border-[var(--line)] p-4"><summary class="cursor-pointer font-semibold">{sectionLabel(section)}</summary><GlobalSettingsPanel {section}/></details>{/each}</div></section>
</div>
