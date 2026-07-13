<script lang="ts">
  import { X } from '@lucide/svelte';
  import GlobalSettingsPanel from './GlobalSettingsPanel.svelte';
  import OutputSettingsPanel from './OutputSettingsPanel.svelte';
  import SettingsPanel from './SettingsPanel.svelte';
  let { section, title, description = '', sessionId, onclose }: { section: string; title: string; description?: string; sessionId?: string; onclose: () => void } = $props();
</script>

<div class="fixed inset-0 z-[75] grid place-items-center bg-black/45 p-3 backdrop-blur-sm" role="presentation" onclick={(event)=>event.target===event.currentTarget&&onclose()}>
  <div class="surface flex max-h-[94vh] w-full max-w-6xl flex-col overflow-hidden rounded-3xl" role="dialog" aria-modal="true" aria-labelledby="settings-modal-title">
    <header class="flex items-start justify-between gap-4 border-b border-[var(--line)] px-6 py-5"><div><div class="eyebrow">{sessionId ? 'Session settings' : 'Application defaults'}</div><h2 id="settings-modal-title" class="mt-1 text-2xl font-semibold">{title}</h2>{#if description}<p class="muted mt-2 max-w-3xl text-sm">{description}</p>{/if}</div><button onclick={onclose} class="rounded-xl p-2" aria-label="Close settings"><X size={20}/></button></header>
    <div class="min-h-0 flex-1 overflow-y-auto bg-[var(--paper)] p-5 sm:p-7">{#if sessionId && section==='output'}<OutputSettingsPanel {sessionId}/>{:else if sessionId}<SettingsPanel {sessionId} {section} {title}/>{:else}<GlobalSettingsPanel {section}/>{/if}</div>
  </div>
</div>
