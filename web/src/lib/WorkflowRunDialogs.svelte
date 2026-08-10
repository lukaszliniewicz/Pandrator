<script lang="ts">
  import { Play, RefreshCw, X } from '@lucide/svelte';
  import type {
    StageRerunImpact,
    StageSettingsMismatch,
    WorkflowStage
  } from './api-models';
  import { artifactRoleLabel } from './artifact-display';
  import { modalFocus } from './modal-focus';

  type PendingRun = {
    stage: WorkflowStage;
    impact: StageRerunImpact;
  };

  type PendingMismatch = {
    stage: WorkflowStage;
    mismatches: StageSettingsMismatch['mismatches'];
  };

  let {
    pendingRun,
    pendingMismatch,
    onclose,
    onrerun,
    onreuse,
    onrefresh
  }: {
    pendingRun: PendingRun | null;
    pendingMismatch: PendingMismatch | null;
    onclose: () => void;
    onrerun: (stage: WorkflowStage) => void | Promise<void>;
    onreuse: (pending: PendingMismatch) => void | Promise<void>;
    onrefresh: (pending: PendingMismatch) => void | Promise<void>;
  } = $props();

  const mismatchFieldLabel = (field: string) =>
    ({
      backend: 'backend',
      target_language: 'target language',
      model: 'model',
      reasoning_effort: 'reasoning level',
      instructions: 'guidance'
    })[field] ?? field;

  const mismatchStageLabel = (key: string) =>
    ({
      clean_source: 'Source cleanup',
      transcribe: 'Transcription',
      correct: 'Correction',
      translate: 'Translation',
      optimize_document: 'Document optimization',
      optimize_tts: 'Speech optimization',
      prepare_text: 'Text preparation'
    })[key] ?? key.replaceAll('_', ' ');

  const mismatchReasonLabel = (reason: string) =>
    ({
      settings_changed: 'Settings changed',
      settings_unverifiable: 'Legacy output has no comparable settings history',
      source_lineage_changed: 'Selected output lineage changed'
    })[reason] ?? reason.replaceAll('_', ' ');
</script>

{#if pendingRun}
  <div
    class="fixed inset-0 z-[75] grid place-items-center bg-black/40 p-5 backdrop-blur-sm"
    role="presentation"
    onclick={(event) => event.target === event.currentTarget && onclose()}
  >
    <!-- svelte-ignore a11y_no_noninteractive_element_to_interactive_role -->
    <section
      use:modalFocus={{ onclose }}
      class="surface w-full max-w-lg rounded-[1.7rem] p-7"
      role="dialog"
      aria-modal="true"
      aria-labelledby="rerun-title"
    >
      <div class="flex items-start justify-between gap-4">
        <div>
          <div class="eyebrow">Create another version</div>
          <h2 id="rerun-title" class="mt-1 text-2xl font-semibold">
            Run {pendingRun.stage.title.toLowerCase()} again?
          </h2>
        </div>
        <button
          onclick={onclose}
          aria-label="Close rerun confirmation"
          class="rounded-lg p-2"><X size={19} /></button
        >
      </div>
      <p class="muted mt-4 text-sm leading-relaxed">
        A new immutable result will be created and selected only after the run
        succeeds. The current version and all work based on it remain saved in
        history.
      </p>
      {#if pendingRun.impact.dependent_selections?.length}
        <div
          class="mt-4 rounded-xl border border-amber-400/40 bg-amber-500/10 p-4 text-sm"
        >
          <strong>Selections that will need a compatible new version</strong>
          <div class="mt-2 flex flex-wrap gap-2">
            {#each pendingRun.impact.dependent_selections as dependent}
              <span
                class="rounded-full bg-[var(--paper-strong)] px-2.5 py-1 text-xs font-semibold"
              >
                {artifactRoleLabel(dependent.role)}
              </span>
            {/each}
          </div>
        </div>
      {/if}
      {#if pendingRun.impact.descendant_total}
        <p class="muted mt-4 text-xs">
          {pendingRun.impact.descendant_total} dependent artifact{pendingRun
            .impact.descendant_total === 1
            ? ''
            : 's'}, including audio takes and exports where applicable, will
          remain available on the earlier path.
        </p>
      {/if}
      <div class="mt-6 flex justify-end gap-2">
        <button
          onclick={onclose}
          class="rounded-xl border border-[var(--line)] px-4 py-2.5 text-sm font-semibold"
          >Cancel</button
        >
        <button
          onclick={() => onrerun(pendingRun.stage)}
          class="flex items-center gap-2 rounded-xl bg-[var(--accent)] px-4 py-2.5 text-sm font-semibold text-white"
        >
          <Play size={16} /> Run and switch when ready
        </button>
      </div>
    </section>
  </div>
{/if}

{#if pendingMismatch}
  <div
    class="fixed inset-0 z-[75] grid place-items-center bg-black/40 p-5 backdrop-blur-sm"
    role="presentation"
    onclick={(event) => event.target === event.currentTarget && onclose()}
  >
    <!-- svelte-ignore a11y_no_noninteractive_element_to_interactive_role -->
    <section
      use:modalFocus={{ onclose }}
      class="surface w-full max-w-lg rounded-[1.7rem] p-7"
      role="dialog"
      aria-modal="true"
      aria-labelledby="mismatch-title"
    >
      <div class="flex items-start justify-between gap-4">
        <div>
          <div class="eyebrow">Before generation</div>
          <h2 id="mismatch-title" class="mt-1 text-2xl font-semibold">
            Choose prerequisite outputs
          </h2>
        </div>
        <button
          onclick={onclose}
          aria-label="Close settings change prompt"
          class="rounded-lg p-2"><X size={19} /></button
        >
      </div>
      <p class="muted mt-4 text-sm leading-relaxed">
        A prerequisite may have different settings, point to a different
        selected-output lineage, or be a legacy result without comparable
        history. Reusing selected outputs skips reruns for the listed stages;
        rerunning prerequisites creates updated outputs before audio generation.
      </p>
      <div class="mt-4 space-y-2">
        {#each pendingMismatch.mismatches as mismatch}
          <div
            class="rounded-xl border border-amber-400/40 bg-amber-500/10 p-4 text-sm"
          >
            <strong>{mismatchStageLabel(mismatch.stage)}</strong>
            <span class="muted">
              — changed: {(mismatch.changed_fields?.length
                ? mismatch.changed_fields
                : ['settings']
              )
                .map(mismatchFieldLabel)
                .join(', ')}
            </span>
            {#if mismatch.reasons?.length}
              <div class="muted mt-1 text-xs">
                {mismatch.reasons.map(mismatchReasonLabel).join(' · ')}
              </div>
            {/if}
          </div>
        {/each}
      </div>
      <div class="mt-6 flex flex-wrap justify-end gap-2">
        <button
          onclick={onclose}
          class="rounded-xl border border-[var(--line)] px-4 py-2.5 text-sm font-semibold"
          >Cancel</button
        >
        <button
          onclick={() => onreuse(pendingMismatch)}
          class="rounded-xl border border-[var(--line)] px-4 py-2.5 text-sm font-semibold"
          aria-label="Reuse all listed prerequisite outputs without rerunning them"
          >Reuse selected outputs</button
        >
        <button
          onclick={() => onrefresh(pendingMismatch)}
          class="flex items-center gap-2 rounded-xl bg-[var(--accent)] px-4 py-2.5 text-sm font-semibold text-white"
        >
          <RefreshCw size={16} /> Rerun prerequisites
        </button>
      </div>
    </section>
  </div>
{/if}
