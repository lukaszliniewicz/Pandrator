<script lang="ts">
  import { onDestroy, untrack } from 'svelte';
  import { page } from '$app/state';
  import { useSessionContext } from '$lib/session-context';
  import { appState } from '$lib/app-state.svelte';
  import PerformancePanel from '$lib/PerformancePanel.svelte';
  import ParameterLabel from '$lib/ParameterLabel.svelte';
  import {
    speechPlanState,
    notifySessionFlowChange,
    type SpeechPlanState
  } from '$lib/session-flow';
  import { errorMessage } from '$lib/errors';

  const context = useSessionContext();
  let plan = $state<SpeechPlanState | null>(null);
  let loading = $state(true);
  let error = $state('');
  let request = 0;
  let loadedSessionId = '';
  async function load(id: string) {
    const serial = ++request;
    loading = true;
    error = '';
    try {
      const result = await speechPlanState(id, { summary: true });
      if (serial === request) plan = result;
    } catch (caught) {
      if (serial === request) error = errorMessage(caught);
    } finally {
      if (serial === request) loading = false;
    }
  }
  $effect(() => {
    const id = context.session?.id;
    if (id && id !== loadedSessionId) {
      loadedSessionId = id;
      plan = null;
      untrack(() => void load(id));
    }
  });
  onDestroy(() => {
    request++;
  });
  const busy = $derived(
    context.workflow.snapshot?.stages.some(
      (stage) => stage.status === 'running'
    ) ||
      appState.jobs.some(
        (job) =>
          job.session_id === context.session?.id &&
          ['queued', 'running', 'cancel_requested'].includes(job.status)
      )
  );
</script>

<section
  class="surface rounded-2xl border border-[var(--line)] p-4 sm:p-6"
  aria-label="Annotation workspace"
>
  <header class="mb-6 flex flex-wrap items-start justify-between gap-4">
    <div class="max-w-3xl">
      <h2 class="text-xl font-semibold">
        <ParameterLabel
          section="speech"
          name="workspace"
          label="Speakers &amp; delivery"
          description="Work through narration blocks in a manual or analysed draft. Speaker labels, dialogue and directions are annotations over the accepted text. Save blocks as you go, review the result, then adopt the draft to use it for future recordings."
        />
      </h2>
      <p class="muted mt-2 text-sm">
        Annotate by hand, or check a draft from AI analysis or a connected MCP
        client. Spoken words and block boundaries stay unchanged.
      </p>
    </div>
    <div class="flex flex-wrap gap-2">
      <a
        class="btn btn-secondary"
        href={`/sessions/${page.params.id}#speech-preparation`}>Speech plan</a
      >
      <a
        class="btn btn-secondary"
        href={`/sessions/${page.params.id}/generation`}>Open Generation</a
      >
    </div>
  </header>
  {#if error}<p class="text-sm text-red-700" role="alert">
      {error}
      <button
        class="btn btn-secondary"
        onclick={() => context.session && void load(context.session.id)}
        >Retry</button
      >
    </p>
  {:else if loading}<p class="muted text-sm" role="status">
      Loading speech plan…
    </p>
  {:else if context.session?.workflow_kind === 'subtitles'}<p class="muted">
      Use Text &amp; subtitles to edit this project’s cues.
    </p>
  {:else if plan?.selected_revision_id && context.session}
    {#if busy}<p
        class="mb-4 rounded-xl border border-[var(--line)] p-3 text-sm"
        role="status"
      >
        Project work is active. You can read existing annotations; editing and
        adoption become available when it finishes.
      </p>{/if}
    {#key context.session.id}
      <PerformancePanel
        workspace
        initialOpen
        sessionId={context.session.id}
        revisionId={plan.selected_revision_id}
        {busy}
        onchanged={() =>
          context.session && notifySessionFlowChange(context.session.id)}
      />
    {/key}
  {:else}<p class="muted text-sm">
      Prepare a speech plan from the project overview first, then return here to
      annotate it.
    </p>{/if}
</section>
