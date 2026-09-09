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
      instructions: 'guidance',
      correction_style: 'correction style',
      char_limit: 'characters per batch',
      max_segments_per_batch: 'segments per batch',
      llm_concurrent_calls: 'parallel requests',
      web_research: 'web research',
      glossary: 'glossary'
    })[field] ?? field.replaceAll('_', ' ');

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
      settings_changed:
        'Processing settings changed since this text was created.',
      settings_unverifiable:
        'This result has no comparable settings record. Pandrator cannot tell whether it needs updating.',
      source_lineage_changed:
        'This text was produced from a different source or earlier selected text.'
    })[reason] ?? reason.replaceAll('_', ' ');

  function changedSetting(
    mismatch: StageSettingsMismatch['mismatches'][number],
    field: string
  ) {
    const label = mismatchFieldLabel(field);
    if (
      !mismatch.stored ||
      !mismatch.current ||
      ['instructions', 'glossary', 'web_research'].includes(field)
    )
      return `${label} changed`;
    const before = mismatch.stored[field];
    const after = mismatch.current[field];
    if (typeof before === 'object' || typeof after === 'object')
      return `${label} changed`;
    const display = (value: unknown) =>
      value === undefined
        ? 'not recorded'
        : value === ''
          ? 'default'
          : typeof value === 'boolean'
            ? value
              ? 'on'
              : 'off'
            : String(value);
    return `${label}: ${display(before)} → ${display(after)}`;
  }
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
      use:modalFocus={{
        onclose,
        initialFocus: '[data-generate-selected-text]'
      }}
      class="surface max-h-[90vh] w-full max-w-xl overflow-y-auto rounded-[1.7rem] p-7"
      role="dialog"
      aria-modal="true"
      aria-labelledby="mismatch-title"
      style:background="var(--paper-strong)"
    >
      <div class="flex items-start justify-between gap-4">
        <div>
          <div class="eyebrow">Before generation</div>
          <h2 id="mismatch-title" class="mt-1 text-2xl font-semibold">
            Generate with the selected text?
          </h2>
        </div>
        <button
          onclick={onclose}
          aria-label="Close settings change prompt"
          class="rounded-lg p-2"><X size={19} /></button
        >
      </div>
      <p class="muted mt-4 text-sm leading-relaxed">
        Your selected text is available for generation. The checks below concern
        how that text was prepared. Generate with it using the current speech
        settings, or refresh the earlier steps first to apply their current
        settings.
      </p>
      <div class="mt-4 space-y-2">
        {#each pendingMismatch.mismatches as mismatch}
          <div
            class="rounded-xl border border-amber-400/40 bg-amber-500/10 p-4 text-sm"
          >
            <strong>{mismatchStageLabel(mismatch.stage)}</strong>
            {#if mismatch.reasons?.length}
              {#each mismatch.reasons as reason}
                <p class="muted mt-1 text-sm">{mismatchReasonLabel(reason)}</p>
              {/each}
            {:else if !mismatch.changed_fields?.length}
              <p class="muted mt-1 text-sm">
                Pandrator could not confirm that this result matches the current
                setup.
              </p>
            {/if}
            {#if mismatch.changed_fields?.length}
              <ul class="mt-2 list-inside list-disc space-y-1 text-xs">
                {#each mismatch.changed_fields as field}
                  <li>{changedSetting(mismatch, field)}</li>
                {/each}
              </ul>
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
          onclick={() => onrefresh(pendingMismatch)}
          class="flex items-center gap-2 rounded-xl border border-[var(--line)] px-4 py-2.5 text-sm font-semibold"
        >
          <RefreshCw size={16} /> Refresh text first
        </button>
        <button
          data-generate-selected-text
          onclick={() => onreuse(pendingMismatch)}
          class="flex items-center gap-2 rounded-xl bg-[var(--accent)] px-4 py-2.5 text-sm font-semibold text-white"
        >
          <Play size={16} /> Generate with selected text
        </button>
      </div>
    </section>
  </div>
{/if}
