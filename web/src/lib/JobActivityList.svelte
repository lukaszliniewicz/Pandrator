<script lang="ts">
  import { Ban, CheckCircle2, ChevronDown, CircleAlert, FileText, LoaderCircle, RefreshCw } from '@lucide/svelte';
  import { api, type JobRecord } from './api';
  import { appState } from './app-state.svelte';

  type JobLog = {
    id: number;
    event_type: string;
    payload_json: Record<string, unknown>;
    created_at: string;
  };

  let { jobs, allowCancel = true }: { jobs: JobRecord[]; allowCancel?: boolean } = $props();
  let expanded = $state<Record<string, boolean>>({});
  let logs = $state<Record<string, JobLog[]>>({});
  let loading = $state<Record<string, boolean>>({});
  let errors = $state<Record<string, string>>({});
  let canceling = $state<Record<string, boolean>>({});

  function eventLabel(event: JobLog) {
    if (event.event_type === 'job.log') return String(event.payload_json.message ?? 'Log message');
    if (event.event_type === 'job.progress') return String(event.payload_json.detail ?? `Progress ${Math.round(Number(event.payload_json.progress ?? 0) * 100)}%`);
    if (event.event_type === 'job.failed') return String(event.payload_json.message ?? 'Job failed');
    return event.event_type.replace(/^job\./, '').replaceAll('_', ' ');
  }

  function eventLevel(event: JobLog) {
    return String(event.payload_json.level ?? (event.event_type === 'job.failed' ? 'ERROR' : 'INFO')).toUpperCase();
  }

  async function loadLogs(id: string, force = false) {
    if (loading[id] || (logs[id] && !force)) return;
    loading[id] = true;
    errors[id] = '';
    try {
      const result = await api<{ items: JobLog[] }>(`/jobs/${id}/logs?limit=2000`);
      logs[id] = result.items;
    } catch (caught) {
      errors[id] = caught instanceof Error ? caught.message : String(caught);
    } finally {
      loading[id] = false;
    }
  }

  async function toggle(id: string) {
    expanded[id] = !expanded[id];
    if (expanded[id]) await loadLogs(id);
  }

  async function cancel(job: JobRecord) {
    if (canceling[job.id]) return;
    canceling[job.id] = true;
    try {
      const updated = await api<JobRecord>(`/jobs/${job.id}/cancel`, { method: 'POST' });
      Object.assign(job, updated);
      await Promise.all([appState.refresh(), expanded[job.id] ? loadLogs(job.id, true) : Promise.resolve()]);
    } catch (caught) {
      errors[job.id] = caught instanceof Error ? caught.message : String(caught);
      expanded[job.id] = true;
    } finally {
      canceling[job.id] = false;
    }
  }
</script>

<div class="surface overflow-hidden rounded-2xl">
  {#each jobs as job (job.id)}
    <article class="border-b border-[var(--line)] last:border-0">
      <div class="flex flex-wrap items-center gap-4 p-4">
        {#if ['running','queued','cancel_requested'].includes(job.status)}
          <LoaderCircle class="animate-spin text-[var(--accent)]" size={19}/>
        {:else if job.status === 'succeeded'}
          <CheckCircle2 class="text-[var(--success)]" size={19}/>
        {:else}
          <CircleAlert class="text-red-500" size={19}/>
        {/if}
        <button onclick={() => toggle(job.id)} class="min-w-0 flex-1 text-left" aria-expanded={Boolean(expanded[job.id])}>
          <div class="flex items-center gap-2"><span class="truncate font-semibold">{job.kind}</span><ChevronDown class={`muted transition-transform ${expanded[job.id] ? 'rotate-180' : ''}`} size={15}/></div>
          <div class="muted mt-1 text-xs">{job.status} - {new Date(job.created_at).toLocaleString()}</div>
          {#if job.error_message}<p class="mt-1 text-xs text-red-500">{job.error_message}</p>{/if}
        </button>
        <div class="w-32"><div class="h-1.5 overflow-hidden rounded-full bg-[var(--line)]"><div class="h-full bg-[var(--accent)]" style={`width:${job.progress * 100}%`}></div></div><div class="muted mt-1 text-right text-[.65rem]">{Math.round(job.progress * 100)}%</div></div>
        <button onclick={() => { expanded[job.id] = true; loadLogs(job.id, true); }} class="btn btn-sm btn-secondary"><FileText size={14}/> Logs</button>
        {#if allowCancel && ['running','queued','cancel_requested'].includes(job.status)}
          <button onclick={() => cancel(job)} disabled={canceling[job.id]} class="btn btn-sm border-red-400/40 text-red-500"><Ban size={14}/> {canceling[job.id] ? 'Canceling...' : 'Cancel'}</button>
        {/if}
      </div>
      {#if expanded[job.id]}
        <div class="job-log border-t border-[var(--line)] bg-[var(--paper)] px-4 py-3">
          <div class="mb-2 flex items-center justify-between gap-3"><strong class="text-xs uppercase tracking-wider">Job log</strong><button onclick={() => loadLogs(job.id, true)} disabled={loading[job.id]} class="btn btn-sm btn-quiet"><RefreshCw class={loading[job.id] ? 'animate-spin' : ''} size={13}/> Refresh</button></div>
          {#if errors[job.id]}<p class="text-xs text-red-500">{errors[job.id]}</p>
          {:else if loading[job.id] && !logs[job.id]}<p class="muted py-4 text-center text-xs">Loading log...</p>
          {:else if logs[job.id]?.length}
            <div class="max-h-96 overflow-auto rounded-xl border border-[var(--line)] bg-[var(--paper-strong)] font-mono text-[.72rem] leading-5">
              {#each logs[job.id] as event (event.id)}
                <div class:error-line={eventLevel(event) === 'ERROR'} class:warning-line={eventLevel(event) === 'WARNING'} class="log-line grid gap-x-3 border-b border-[var(--line)] px-3 py-2 last:border-0 sm:grid-cols-[10rem_4.5rem_1fr]">
                  <time class="muted">{new Date(event.created_at).toLocaleString()}</time><span class="font-bold">{eventLevel(event)}</span><span class="whitespace-pre-wrap break-words">{eventLabel(event)}{#if event.payload_json.trace}<span class="mt-1 block whitespace-pre-wrap text-red-500">{String(event.payload_json.trace)}</span>{/if}</span>
                </div>
              {/each}
            </div>
          {:else}<p class="muted py-4 text-center text-xs">No log entries were recorded.</p>{/if}
        </div>
      {/if}
    </article>
  {:else}
    <div class="muted p-10 text-center">No jobs recorded.</div>
  {/each}
</div>

<style>
  .error-line{background:color-mix(in srgb,#ef4444 8%,transparent);color:#dc2626}.warning-line{background:color-mix(in srgb,#f59e0b 8%,transparent)}
  @media(max-width:640px){.log-line{grid-template-columns:1fr}.job-log{padding-inline:.65rem}}
</style>
