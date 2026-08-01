<script lang="ts">
  import { errorMessage } from '$lib/errors';
  import { page } from '$app/state';
  import { Check, Play, Square } from '@lucide/svelte';
  import { jobApi, sessionApi } from '$lib/domain-api';
  import type { AgentRun, AgentStep, SessionSource } from '$lib/api-models';
  import SettingsPanel from '$lib/SettingsPanel.svelte';

  const sessionId = String(page.params.id);
  let sources = $state<SessionSource[]>([]);
  let runs = $state<AgentRun[]>([]);
  let selectedSource = $state('');
  let selectedRun = $state<AgentRun | null>(null);
  let steps = $state<AgentStep[]>([]);
  let error = $state('');
  let sourceProfile = $state<string | null>(null);

  async function load() {
    const [sourcePayload, outputSettings] = await Promise.all([
      sessionApi.sources(sessionId),
      sessionApi.settings(sessionId, 'output')
    ]);
    sourceProfile = String(outputSettings.context?.source_profile ?? 'none');
    if (sourceProfile !== 'document') {
      sources = [];
      return;
    }
    sources = sourcePayload.items.filter(
      (item) => Boolean(item.artifact_id) && item.attachment.is_current
    );
    selectedSource ||=
      sources.find((item) => item.attachment.is_current)?.artifact_id ??
      sources[0]?.artifact_id ??
      '';
    runs = (await sessionApi.agentRuns(sessionId, 'source_cleaning')).items;
    if (runs[0]) await selectRun(runs[0]);
  }
  async function selectRun(run: AgentRun) {
    selectedRun = run;
    steps = (await sessionApi.agentSteps(run.id)).items;
  }
  async function run() {
    try {
      const settings = await sessionApi.settings(sessionId, 'source_cleaning');
      const created = await sessionApi.createAgentRun(
        sessionId,
        selectedSource,
        settings.effective
      );
      selectedRun = created;
      steps = [];
      await load();
    } catch (caught) {
      error = errorMessage(caught);
    }
  }
  async function cancel() {
    if (selectedRun?.job_id) await jobApi.cancel(selectedRun.job_id);
  }
  async function accept() {
    if (selectedRun) {
      selectedRun = await sessionApi.acceptAgentRun(selectedRun.id);
      await load();
    }
  }
  function warningsFor(step: AgentStep) {
    const warnings = step.output_json?.warnings;
    return Array.isArray(warnings) ? warnings.map(String) : [];
  }

  load().catch((caught) => {
    error = errorMessage(caught);
    sourceProfile = 'none';
  });
</script>

{#if sourceProfile === null}
  <section class="surface rounded-2xl p-6" aria-busy="true">
    <p class="muted text-sm">Loading source cleaning…</p>
  </section>
{:else if sourceProfile !== 'document'}
  <section class="surface rounded-2xl p-6">
    <h2 class="text-xl font-semibold">Cleaning is for document sources</h2>
    <p class="muted mt-2 max-w-2xl text-sm">
      Audio, video, and subtitle sources already have structured timing or media
      content, so document extraction and text-cleaning controls do not apply.
    </p>
    <a href={`/sessions/${sessionId}/sources`} class="action mt-4 inline-flex"
      >Review sources</a
    >
  </section>
{:else}<div class="space-y-5">
    <div class="flex flex-wrap items-end justify-between gap-4">
      <div>
        <h2 class="text-2xl font-semibold">Source cleaning</h2>
        <p class="muted mt-2">
          Inspect deterministic extraction and the agentic phase loop without
          exposing private model reasoning.
        </p>
      </div>
      <div class="flex gap-2">
        <select
          bind:value={selectedSource}
          class="rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2 text-sm"
          >{#each sources as source}<option value={source.artifact_id}
              >{source.display_name}</option
            >{/each}</select
        >{#if selectedRun && ['queued', 'running'].includes(selectedRun.status)}<button
            onclick={cancel}
            class="action text-red-500"><Square size={15} /> Stop</button
          >{:else}<button
            onclick={run}
            disabled={!selectedSource}
            class="action bg-[var(--accent)] text-white disabled:opacity-40"
            ><Play size={15} /> Run cleaning</button
          >{/if}
      </div>
    </div>
    {#if error}<p class="text-sm text-red-500">{error}</p>{/if}<SettingsPanel
      {sessionId}
      section="source_cleaning"
      title="Extraction and agentic loop"
    />
    <div class="grid gap-5 lg:grid-cols-[18rem_1fr]">
      <aside class="surface rounded-2xl p-4">
        <div class="eyebrow mb-3">Runs</div>
        {#each runs as item}<button
            onclick={() => selectRun(item)}
            class:active={selectedRun?.id === item.id}
            class="run"
            ><strong>{item.status}</strong><span
              >{new Date(item.created_at ?? '').toLocaleString()}</span
            ></button
          >{:else}<p class="muted text-sm">No agentic runs yet.</p>{/each}
      </aside>
      <section class="surface rounded-2xl p-5">
        <div class="flex justify-between">
          <div>
            <div class="eyebrow">Agent audit</div>
            <p class="muted mt-2 text-sm">
              Phase summaries, operations, validation warnings, tool results,
              and costs. Chain-of-thought is never stored.
            </p>
          </div>
          {#if selectedRun?.status === 'completed'}<button
              onclick={accept}
              class="action"><Check size={14} /> Accept result</button
            >{/if}
        </div>
        <div class="mt-5 space-y-3">
          {#each steps as step}<article
              class="rounded-xl border border-[var(--line)] p-4"
            >
              <div class="flex justify-between">
                <strong>{step.phase}</strong><span class="muted text-xs"
                  >{step.status}</span
                >
              </div>
              <p class="muted mt-2 text-sm">{step.summary}</p>
              {#if warningsFor(step).length}<ul
                  class="mt-3 list-disc pl-5 text-xs text-[var(--warning)]"
                >
                  {#each warningsFor(step) as warning}<li>{warning}</li>{/each}
                </ul>{/if}
            </article>{:else}<p class="muted text-sm">
              Select a completed run to inspect its structured audit.
            </p>{/each}
        </div>
      </section>
    </div>
  </div>{/if}

<style>
  .action {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    border: 1px solid var(--line);
    border-radius: 0.75rem;
    padding: 0.65rem 0.8rem;
    font-size: 0.78rem;
    font-weight: 700;
  }
  .run {
    display: block;
    width: 100%;
    border-radius: 0.7rem;
    padding: 0.7rem;
    text-align: left;
  }
  .run:hover,
  .run.active {
    background: var(--accent-soft);
  }
  .run strong,
  .run span {
    display: block;
    font-size: 0.72rem;
  }
  .run span {
    margin-top: 0.2rem;
    color: var(--muted);
    font-weight: 400;
  }
</style>
