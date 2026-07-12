<script lang="ts">
  import { page } from '$app/state';
  import { Download, FileAudio, FileVideo, LoaderCircle, PackageCheck } from '@lucide/svelte';
  import { api } from '$lib/api';
  import SettingsPanel from '$lib/SettingsPanel.svelte';

  const sessionId=String(page.params.id);
  let artifacts=$state<any[]>([]);let busy=$state(false);let message=$state('');let error=$state('');
  async function load(){artifacts=(await api<{items:any[]}>(`/artifacts?session_id=${sessionId}&limit=300`)).items.filter((item:any)=>['export','audiobook_audio','dubbing_audio'].includes(item.role))}
  async function assemble(){busy=true;error='';try{const job=await api<any>(`/sessions/${sessionId}/stages/export/run`,{method:'POST',body:'{}'});message=`Export queued as ${job.id.slice(0,8)}.`}catch(caught){error=caught instanceof Error?caught.message:String(caught)}finally{busy=false}}
  load();
</script>
<div class="space-y-5"><div class="flex flex-wrap items-end justify-between gap-4"><div><h2 class="text-2xl font-semibold">Output</h2><p class="muted mt-2">Bilingual output is optional. Available choices depend on which source, correction, translation, and audio artifacts exist.</p></div><button onclick={assemble} disabled={busy} class="flex items-center gap-2 rounded-xl bg-[var(--accent)] px-4 py-2.5 text-sm font-semibold text-white disabled:opacity-50">{#if busy}<LoaderCircle class="animate-spin" size={16}/>{:else}<PackageCheck size={16}/>{/if} Assemble export</button></div>{#if message}<p class="rounded-xl bg-[var(--accent-soft)] p-3 text-sm">{message}</p>{/if}{#if error}<p class="rounded-xl bg-red-500/10 p-3 text-sm text-red-500">{error}</p>{/if}<SettingsPanel {sessionId} section="output" title="Export and metadata"/><section class="surface rounded-2xl p-5"><div class="eyebrow">Completed outputs</div><div class="mt-4 grid gap-3 md:grid-cols-2">{#each artifacts as artifact}<a href={`/api/v1/artifacts/${artifact.id}/content`} class="flex items-center gap-3 rounded-xl border border-[var(--line)] p-4">{#if artifact.kind==='video'}<FileVideo size={18}/>{:else}<FileAudio size={18}/>{/if}<div class="min-w-0 flex-1"><div class="truncate font-semibold">{artifact.role}</div><div class="muted truncate text-xs">{artifact.relative_path}</div></div><Download size={16}/></a>{:else}<p class="muted text-sm">No completed outputs yet.</p>{/each}</div></section></div>
