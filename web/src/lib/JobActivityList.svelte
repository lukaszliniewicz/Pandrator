<script lang="ts">
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { errorMessage } from './errors';
  import {
    Ban,
    CheckCircle2,
    ChevronDown,
    CircleAlert,
    FileText,
    LoaderCircle,
    Play,
    RefreshCw
  } from '@lucide/svelte';
  import type { JobLogRecord as JobLog, JobRecord } from './api-models';
  import { appState } from './app-state.svelte';
  import { generationApi, jobApi } from './domain-api';
  import { jobProgressDetail } from './job-progress';
  import GenerationStartDialog from './GenerationStartDialog.svelte';

  let {
    jobs,
    allowCancel = true
  }: { jobs: JobRecord[]; allowCancel?: boolean } = $props();
  let expanded = $state<Record<string, boolean>>({});
  let logs = $state<Record<string, JobLog[]>>({});
  let loading = $state<Record<string, boolean>>({});
  let errors = $state<Record<string, string>>({});
  let canceling = $state<Record<string, boolean>>({});
  let recovering = $state('');
  let recovery = $state<{
    sessionId: string;
    planRevisionId: string;
    runId: string;
    projectName: string;
    pausedRunLabel: string | null;
  } | null>(null);
  let recoveryController: AbortController | undefined;
  onMount(() => () => recoveryController?.abort());
  const projects = $derived(
    new Map(appState.sessions.map((item) => [item.id, item]))
  );

  function projectFor(job: JobRecord) {
    if (job.project) return job.project;
    const id = job.session_id || job.payload_json?.session_id;
    if (typeof id !== 'string' || !id) return null;
    const project = projects.get(id);
    return {
      id,
      name: project?.name ?? null,
      available:
        Boolean(project) &&
        !project?.trashed_at &&
        project?.status !== 'purging',
      trashed: Boolean(project?.trashed_at)
    };
  }

  function continuationRun(job: JobRecord) {
    const project = projectFor(job);
    const runId = job.generation_run_id || job.payload_json?.generation_run_id;
    if (
      job.kind !== 'generation.run' ||
      job.status !== 'failed' ||
      !project?.available ||
      typeof runId !== 'string' ||
      !runId
    )
      return null;
    return runId;
  }

  async function openContinuation(job: JobRecord) {
    const project = projectFor(job);
    const runId = continuationRun(job);
    if (!project || !runId || recovering || recovery) return;
    recovering = job.id;
    errors[job.id] = '';
    recoveryController?.abort();
    const controller = new AbortController();
    recoveryController = controller;
    try {
      const [source, summary] = await Promise.all([
        generationApi.run(runId, controller.signal),
        generationApi.summary(project.id, controller.signal)
      ]);
      if (controller.signal.aborted) return;
      if (source.session_id !== project.id || source.status !== 'failed')
        throw new Error(
          'This generation is no longer a failed run in this project. Open the project to check its current state.'
        );
      if (
        summary.active_run &&
        [
          'queued',
          'running',
          'pausing',
          'pause_requested',
          'cancel_requested'
        ].includes(summary.active_run.status)
      )
        throw new Error(
          'This project already has generation in progress. Open the project to follow it.'
        );
      if (!summary.plan_revision_id)
        throw new Error(
          'No active speech plan is available. Open the project to prepare its speech plan.'
        );
      recovery = {
        sessionId: project.id,
        planRevisionId: summary.plan_revision_id,
        runId: source.id,
        projectName: project.name || 'this project',
        pausedRunLabel:
          summary.active_run?.status === 'paused' ? 'the paused run' : null
      };
    } catch (caught) {
      if (!controller.signal.aborted) {
        errors[job.id] = errorMessage(caught);
        expanded[job.id] = true;
      }
    } finally {
      if (recoveryController === controller) recovering = '';
    }
  }

  async function continuationStarted() {
    const sessionId = recovery?.sessionId;
    recovery = null;
    if (sessionId)
      await goto(
        `/sessions/${encodeURIComponent(sessionId)}/generation?view=active`
      );
    void appState.refresh().catch(() => undefined);
  }

  function eventLabel(event: JobLog) {
    if (event.event_type === 'job.log')
      return String(event.payload_json.message ?? 'Log message');
    if (event.event_type === 'job.progress')
      return String(
        event.payload_json.detail ??
          `Progress ${Math.round(Number(event.payload_json.progress ?? 0) * 100)}%`
      );
    if (event.event_type === 'job.failed')
      return String(event.payload_json.message ?? 'Job failed');
    return event.event_type.replace(/^job\./, '').replaceAll('_', ' ');
  }

  function eventLevel(event: JobLog) {
    return String(
      event.payload_json.level ??
        (event.event_type === 'job.failed' ? 'ERROR' : 'INFO')
    ).toUpperCase();
  }

  function progressPercent(job: JobRecord) {
    const value = Number(job.progress ?? 0);
    return Math.round(
      Math.max(0, Math.min(1, Number.isFinite(value) ? value : 0)) * 100
    );
  }

  function currentDetail(job: JobRecord) {
    return jobProgressDetail(job);
  }

  async function loadLogs(id: string, force = false) {
    if (loading[id] || (logs[id] && !force)) return;
    loading[id] = true;
    errors[id] = '';
    try {
      const result = await jobApi.logs(id);
      logs[id] = result.items;
    } catch (caught) {
      errors[id] = errorMessage(caught);
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
      const updated = await jobApi.cancel(job.id);
      Object.assign(job, updated);
      await Promise.all([
        appState.refresh(),
        expanded[job.id] ? loadLogs(job.id, true) : Promise.resolve()
      ]);
    } catch (caught) {
      errors[job.id] = errorMessage(caught);
      expanded[job.id] = true;
    } finally {
      canceling[job.id] = false;
    }
  }
</script>

<div class="surface overflow-hidden rounded-2xl">
  {#each jobs as job (job.id)}
    {@const project = projectFor(job)}
    {@const continuation = continuationRun(job)}
    <article class="border-b border-[var(--line)] last:border-0">
      <div class="flex flex-wrap items-start gap-4 p-4 lg:items-center">
        {#if ['running', 'queued', 'cancel_requested'].includes(job.status)}
          <LoaderCircle class="animate-spin text-[var(--accent)]" size={19} />
        {:else if job.status === 'succeeded'}
          <CheckCircle2 class="text-[var(--success)]" size={19} />
        {:else}
          <CircleAlert class="text-red-500" size={19} />
        {/if}
        <div class="min-w-0 flex-1">
          <button
            onclick={() => toggle(job.id)}
            class="w-full text-left"
            aria-expanded={Boolean(expanded[job.id])}
          >
            <div class="flex items-center gap-2">
              <span class="truncate font-semibold"
                >{job.kind === 'generation.run'
                  ? 'Audio generation'
                  : job.kind}</span
              ><ChevronDown
                class={`muted transition-transform ${expanded[job.id] ? 'rotate-180' : ''}`}
                size={15}
              />
            </div>
            <div class="muted mt-1 text-xs">
              {job.status} - {new Date(job.created_at).toLocaleString()}
            </div>
            {#if currentDetail(job)}<p
                class="muted mt-1 truncate text-xs"
                title={currentDetail(job)}
              >
                {currentDetail(job)}
              </p>{/if}
            {#if job.error_message}<p
                class="mt-1 break-words text-xs text-red-500 [overflow-wrap:anywhere]"
              >
                {job.error_message}
              </p>{/if}
          </button>
          {#if project}
            <div
              class="muted mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs"
            >
              <span
                class="break-words [overflow-wrap:anywhere] lg:truncate"
                title={project.name || project.id}
              >
                {project.name || `Project ${project.id.slice(0, 8)}`}
              </span>
              {#if project.available}
                <a
                  class="font-semibold text-[var(--accent)]"
                  href={`/sessions/${encodeURIComponent(project.id)}`}
                  >Open project</a
                >
              {:else if project.trashed}
                <span>In Trash — restore the project to continue.</span>
                <a class="font-semibold text-[var(--accent)]" href="/sessions"
                  >Open projects</a
                >
              {:else}
                <span>Project unavailable</span>
              {/if}
            </div>
          {/if}
        </div>
        <div class="flex w-full flex-wrap items-center gap-3 lg:contents">
          <div
            class="min-w-24 flex-1 lg:w-32 lg:flex-none"
            role="progressbar"
            aria-label={`${job.kind} progress`}
            aria-valuemin="0"
            aria-valuemax="100"
            aria-valuenow={progressPercent(job)}
          >
            <div class="h-1.5 overflow-hidden rounded-full bg-[var(--line)]">
              <div
                class="h-full bg-[var(--accent)] transition-[width]"
                style={`width:${progressPercent(job)}%`}
              ></div>
            </div>
            <div class="muted mt-1 text-right text-[.65rem]">
              {progressPercent(job)}%
            </div>
          </div>
          <button
            onclick={() => {
              expanded[job.id] = true;
              loadLogs(job.id, true);
            }}
            class="btn btn-sm btn-secondary"><FileText size={14} /> Logs</button
          >
          {#if continuation}
            <button
              onclick={() => openContinuation(job)}
              disabled={Boolean(recovering) || Boolean(recovery)}
              class="btn btn-sm btn-secondary"
              aria-label={`Continue unfinished audio for ${project?.name || 'this project'}`}
              ><Play size={14} />
              {recovering === job.id
                ? 'Loading continuation…'
                : 'Continue unfinished audio…'}</button
            >
          {/if}
          {#if allowCancel && ['running', 'queued', 'cancel_requested'].includes(job.status)}
            <button
              onclick={() => cancel(job)}
              disabled={canceling[job.id]}
              class="btn btn-sm border-red-400/40 text-red-500"
              ><Ban size={14} />
              {canceling[job.id] ? 'Canceling...' : 'Cancel'}</button
            >
          {/if}
        </div>
      </div>
      {#if expanded[job.id]}
        <div
          class="job-log border-t border-[var(--line)] bg-[var(--paper)] px-4 py-3"
        >
          <div class="mb-2 flex items-center justify-between gap-3">
            <strong class="text-xs uppercase tracking-wider">Job log</strong
            ><button
              onclick={() => loadLogs(job.id, true)}
              disabled={loading[job.id]}
              class="btn btn-sm btn-quiet"
              ><RefreshCw
                class={loading[job.id] ? 'animate-spin' : ''}
                size={13}
              /> Refresh</button
            >
          </div>
          {#if errors[job.id]}<p class="text-xs text-red-500" role="alert">
              {errors[job.id]}
            </p>
          {:else if loading[job.id] && !logs[job.id]}<p
              class="muted py-4 text-center text-xs"
            >
              Loading log...
            </p>
          {:else if logs[job.id]?.length}
            <div
              class="max-h-96 overflow-auto rounded-xl border border-[var(--line)] bg-[var(--paper-strong)] font-mono text-[.72rem] leading-5"
            >
              {#each logs[job.id] as event (event.id)}
                <div
                  class:error-line={eventLevel(event) === 'ERROR'}
                  class:warning-line={eventLevel(event) === 'WARNING'}
                  class="log-line grid gap-x-3 border-b border-[var(--line)] px-3 py-2 last:border-0 sm:grid-cols-[10rem_4.5rem_1fr]"
                >
                  <time class="muted"
                    >{new Date(event.created_at).toLocaleString()}</time
                  ><span class="font-bold">{eventLevel(event)}</span><span
                    class="whitespace-pre-wrap break-words"
                    >{eventLabel(event)}{#if event.payload_json.trace}<span
                        class="mt-1 block whitespace-pre-wrap text-red-500"
                        >{String(event.payload_json.trace)}</span
                      >{/if}</span
                  >
                </div>
              {/each}
            </div>
          {:else}<p class="muted py-4 text-center text-xs">
              No log entries were recorded.
            </p>{/if}
        </div>
      {/if}
    </article>
  {:else}
    <div class="muted p-10 text-center">No jobs recorded.</div>
  {/each}
</div>

{#if recovery}
  <GenerationStartDialog
    sessionId={recovery.sessionId}
    planRevisionId={recovery.planRevisionId}
    planLabel={recovery.projectName}
    settingsSourceRunId={recovery.runId}
    initialMode="continue"
    pausedRunLabel={recovery.pausedRunLabel}
    onclose={() => (recovery = null)}
    onstarted={continuationStarted}
  />
{/if}

<style>
  .error-line {
    background: color-mix(in srgb, #ef4444 8%, transparent);
    color: #dc2626;
  }
  .warning-line {
    background: color-mix(in srgb, #f59e0b 8%, transparent);
  }
  @media (max-width: 640px) {
    .log-line {
      grid-template-columns: 1fr;
    }
    .job-log {
      padding-inline: 0.65rem;
    }
  }
</style>
