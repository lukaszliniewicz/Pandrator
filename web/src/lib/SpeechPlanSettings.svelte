<script lang="ts">
  import { LoaderCircle, Save, X } from '@lucide/svelte';
  import { onMount, tick } from 'svelte';
  import { sessionApi } from './domain-api';
  import type { SettingsPayload } from './api-models';
  import { errorMessage } from './errors';
  import { modalDialog } from './modal-dialog';
  import ParameterLabel from './ParameterLabel.svelte';
  import {
    sessionFlowAction,
    speechPlanState,
    type SpeechPlanState
  } from './session-flow';

  let {
    sessionId,
    plan,
    onclose,
    onsaved
  }: {
    sessionId: string;
    plan: SpeechPlanState | null;
    onclose: () => void;
    onsaved: () => Promise<unknown>;
  } = $props();
  type NumericControl = {
    key: string;
    label: string;
    fallback: number;
    min: number;
    max?: number;
  };
  const controls: NumericControl[] = [
    {
      key: 'speech_block_min_chars',
      label: 'Preferred minimum split size',
      fallback: 10,
      min: 1
    },
    {
      key: 'speech_block_max_chars',
      label: 'Maximum characters',
      fallback: 220,
      min: 1
    },
    {
      key: 'speech_block_merge_threshold',
      label: 'Speech-block merge gap (ms)',
      fallback: 1500,
      min: 0
    },
    {
      key: 'speech_block_continuation_threshold_ms',
      label: 'Maximum unfinished-phrase pause (ms)',
      fallback: 3000,
      min: 0
    },
    {
      key: 'speech_block_max_internal_gap_ms',
      label: 'Normal silence limit inside a block (ms)',
      fallback: 4000,
      min: 0
    }
  ];
  const repairControls: NumericControl[] = [
    {
      key: 'speech_block_early_repair_min_shortfall_ms',
      label: 'Finish early by at least (ms)',
      fallback: 1000,
      min: 100,
      max: 60000
    },
    {
      key: 'speech_block_early_repair_min_shortfall_percent',
      label: 'Finish early by at least (%)',
      fallback: 20,
      min: 1,
      max: 95
    },
    {
      key: 'speech_block_early_repair_min_advance_ms',
      label: 'Minimum timing improvement (ms)',
      fallback: 1000,
      min: 100,
      max: 60000
    },
    {
      key: 'speech_block_early_repair_min_child_span_ms',
      label: 'Minimum cue span per new block (ms)',
      fallback: 1000,
      min: 250,
      max: 60000
    }
  ];
  const allControls = [...controls, ...repairControls];
  let stored = $state<SettingsPayload | null>(null);
  let values = $state<Record<string, number>>({});
  let earlyRepairEnabled = $state(false);
  let saving = $state(false);
  let error = $state('');
  let step = $state<'edit' | 'unsaved' | 'prepare'>('edit');
  let dialog: HTMLDialogElement;
  let preparationPlanId: string | null = null;
  let preparationInputId: string | null = null;
  let alive = true;
  const changed = (control: NumericControl) =>
    values[control.key] !==
    Number(stored?.effective[control.key] ?? control.fallback);
  const blockBuildingChanged = $derived(
    Boolean(stored && controls.some(changed))
  );
  const dirty = $derived(
    Boolean(
      stored &&
      (allControls.some(changed) ||
        earlyRepairEnabled !==
          (stored.effective.speech_block_early_repair_enabled === true))
    )
  );
  const title = $derived(
    step === 'unsaved'
      ? 'Save your block settings?'
      : step === 'prepare'
        ? 'Prepare a new speech plan?'
        : 'Speech-block settings'
  );
  const valid = $derived(
    allControls.every(
      ({ key, min, max }) =>
        Number.isInteger(values[key]) &&
        values[key] >= min &&
        (max === undefined || values[key] <= max)
    ) && values.speech_block_max_chars >= values.speech_block_min_chars
  );
  onMount(() => {
    void sessionApi
      .settings(sessionId, 'tts')
      .then((result) => {
        if (!alive) return;
        stored = result;
        earlyRepairEnabled =
          result.effective.speech_block_early_repair_enabled === true;
        values = Object.fromEntries(
          allControls.map(({ key, fallback }) => [
            key,
            Number(result.effective[key] ?? fallback)
          ])
        );
      })
      .catch((caught) => {
        if (alive) error = errorMessage(caught);
      });
    return () => {
      alive = false;
    };
  });
  async function showStep(next: typeof step) {
    step = next;
    await tick();
    dialog.querySelector<HTMLElement>('#plan-settings-title')?.focus();
  }
  function requestClose() {
    if (saving) return;
    if (step === 'unsaved') void showStep('edit');
    else if (step === 'edit' && dirty) void showStep('unsaved');
    else onclose();
  }
  async function save() {
    if (!stored || !valid || saving) return;
    const offerPreparation =
      blockBuildingChanged && Boolean(plan?.selected_revision_id);
    preparationPlanId = plan?.selected_revision_id ?? null;
    preparationInputId = plan?.current_input?.artifact_id ?? null;
    saving = true;
    error = '';
    try {
      const saved = await sessionApi.saveSettings(
        sessionId,
        'tts',
        stored.revision,
        {
          ...stored.override,
          ...values,
          speech_block_early_repair_enabled: earlyRepairEnabled
        }
      );
      stored = {
        ...stored,
        ...saved,
        effective: {
          ...stored.effective,
          ...values,
          speech_block_early_repair_enabled: earlyRepairEnabled
        }
      };
      await onsaved();
      if (offerPreparation) await showStep('prepare');
      else onclose();
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      saving = false;
    }
  }
  async function prepare() {
    if (saving) return;
    saving = true;
    error = '';
    try {
      const current = await speechPlanState(sessionId);
      if (
        current.selected_revision_id !== preparationPlanId ||
        current.current_input?.artifact_id !== preparationInputId
      )
        throw new Error(
          'The selected plan or text changed. Your settings are saved; close this dialog and review the current selection before preparing a new plan.'
        );
      if (current.blocked_reason || !current.can_prepare)
        throw new Error(
          current.blocked_reason ||
            'Select input text before preparing a new plan.'
        );
      await sessionFlowAction(sessionId, 'generation-plan/prepare', {
        expected_revision: current.session_revision,
        expected_plan_revision_id: current.selected_revision_id,
        source_artifact_id: current.current_input?.artifact_id
      });
      await onsaved();
      onclose();
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      saving = false;
    }
  }
</script>

<dialog
  bind:this={dialog}
  use:modalDialog={{ onclose: requestClose }}
  aria-labelledby="plan-settings-title"
  class="surface flex max-h-[90dvh] flex-col overflow-hidden rounded-3xl p-0 text-[var(--ink)]"
>
  <div class="modal-scroll p-6 sm:p-7">
    <header class="flex items-start justify-between gap-4">
      <div>
        <h2
          id="plan-settings-title"
          tabindex="-1"
          class="text-xl font-semibold focus:outline-none"
        >
          {title}
        </h2>
        {#if step === 'edit'}<p class="muted mt-2 text-sm">
            Control how subtitle text becomes speech blocks. Block sizes apply
            when you prepare a new plan. Repair settings apply to the next full
            generation run, including with the current plan.
          </p>{/if}
      </div>
      <button
        class="btn btn-icon"
        aria-label={step === 'unsaved'
          ? 'Keep editing block settings'
          : 'Close speech-block settings'}
        disabled={saving}
        onclick={requestClose}><X size={18} /></button
      >
    </header>
    {#if error}<p class="mt-4 text-sm text-red-600" role="alert">
        {error}
      </p>{/if}
    {#if !stored && !error}<p class="muted mt-4" role="status">
        Loading settings…
      </p>{/if}
    {#if step === 'unsaved'}
      <p class="muted mt-4 text-sm">
        Your changes haven’t been saved. Save them for this session, or discard
        them and keep the previous settings.
      </p>
      {#if !valid}<p class="mt-3 text-sm text-amber-700" role="status">
          Some values are invalid. Return to editing to correct them before
          saving.
        </p>{/if}
      <footer
        class="mt-6 grid gap-2 border-t border-[var(--line)] pt-4 sm:flex sm:flex-wrap sm:justify-end"
      >
        <button
          class="btn btn-secondary"
          disabled={saving}
          onclick={() => showStep('edit')}>Keep editing</button
        >
        <button class="btn btn-secondary" disabled={saving} onclick={onclose}
          >Discard changes</button
        >
        <button
          class="btn btn-primary"
          disabled={saving || !valid}
          onclick={save}
        >
          {#if saving}<LoaderCircle
              size={16}
              class="animate-spin"
            />{:else}<Save size={16} />{/if}Save changes
        </button>
      </footer>
    {:else if step === 'prepare'}
      <p class="mt-4 text-sm font-semibold" role="status">
        Your settings are saved.
      </p>
      <p class="muted mt-3 text-sm">
        Prepare and select a new plan to apply the new block sizes and grouping.
        The current version, including manual edits and audio takes, stays in
        history. Preparation does not start audio generation.
      </p>
      <p class="muted mt-3 text-sm">
        You can also keep the current plan and use these settings when you
        prepare a new one later.
      </p>
      {#if plan?.blocked_reason}<p
          class="mt-3 text-sm text-amber-700"
          role="status"
        >
          {plan.blocked_reason} Your saved settings can be used once this work finishes.
        </p>{/if}
      <footer
        class="mt-6 grid gap-2 border-t border-[var(--line)] pt-4 sm:flex sm:flex-wrap sm:justify-end"
      >
        <button class="btn btn-secondary" disabled={saving} onclick={onclose}
          >Keep current plan</button
        >
        <button
          class="btn btn-primary"
          disabled={saving ||
            !plan?.can_prepare ||
            Boolean(plan.blocked_reason)}
          onclick={prepare}
        >
          {#if saving}<LoaderCircle
              size={16}
              class="animate-spin"
            />{/if}Prepare a new plan
        </button>
      </footer>
    {:else}<form
        onsubmit={(event) => {
          event.preventDefault();
          void save();
        }}
      >
        <fieldset
          disabled={!stored || saving}
          class="mt-5 grid gap-4 sm:grid-cols-2"
        >
          {#each controls as control}
            <label class="text-sm font-semibold"
              ><ParameterLabel
                section="tts"
                name={control.key}
                label={control.label}
                compact
              />
              <input
                type="number"
                min={control.min}
                step="1"
                required
                bind:value={values[control.key]}
                class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2.5 font-normal"
              /></label
            >
          {/each}
          <label
            class="flex items-start gap-3 rounded-xl border border-[var(--line)] p-4 text-sm sm:col-span-2"
          >
            <input
              type="checkbox"
              bind:checked={earlyRepairEnabled}
              class="mt-1"
              aria-describedby="early-repair-description"
            />
            <span>
              <span class="font-semibold"
                >Reduce speech getting ahead of subtitles</span
              >
              <span id="early-repair-description" class="muted mt-1 block">
                Regenerate combined blocks as smaller parts when later phrases
                are spoken before their subtitles. Keep short blocks together
                when they help playback catch up.
              </span>
              <span class="muted mt-2 block text-xs">
                Uses extra speech generation. You can return to the original
                version.
              </span>
            </span>
          </label>
          <details
            class="rounded-xl border border-[var(--line)] p-4 sm:col-span-2"
          >
            <summary class="cursor-pointer text-sm font-semibold"
              >Repair thresholds</summary
            >
            <p class="muted my-3 text-sm">
              Both early-finish thresholds must be met. Lower values allow more
              splits. Blocks needed for catch-up always stay together.
            </p>
            <div class="grid gap-4 sm:grid-cols-2">
              {#each repairControls as control}
                <label class="text-sm font-semibold">
                  <ParameterLabel
                    section="tts"
                    name={control.key}
                    label={control.label}
                    compact
                  />
                  <input
                    type="number"
                    min={control.min}
                    max={control.max}
                    step="1"
                    required
                    bind:value={values[control.key]}
                    class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2.5 font-normal"
                  />
                </label>
              {/each}
            </div>
          </details>
        </fieldset>
        <footer
          class="mt-6 flex flex-wrap items-center justify-end gap-2 border-t border-[var(--line)] pt-4"
        >
          {#if dirty}<span class="muted mr-auto text-sm" role="status"
              >Unsaved changes</span
            >{/if}
          <button
            type="button"
            class="btn btn-secondary"
            disabled={saving}
            onclick={requestClose}>Cancel</button
          >
          <button
            type="submit"
            class="btn btn-primary"
            disabled={!stored || !valid || saving}
          >
            {#if saving}<LoaderCircle
                size={16}
                class="animate-spin"
              />{:else}<Save size={16} />{/if}Save block settings</button
          >
        </footer>
      </form>{/if}
  </div>
</dialog>

<style>
  dialog {
    width: min(36rem, calc(100vw - 2rem));
    max-width: none;
    margin: auto;
    background: var(--paper-strong);
  }

  dialog::backdrop {
    background: rgb(0 0 0 / 35%);
    backdrop-filter: blur(4px);
  }
</style>
