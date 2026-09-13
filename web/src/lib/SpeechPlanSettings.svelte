<script lang="ts">
  import { LoaderCircle, Save, X } from '@lucide/svelte';
  import { onMount } from 'svelte';
  import { sessionApi } from './domain-api';
  import type { SettingsPayload } from './api-models';
  import { errorMessage } from './errors';
  import { modalFocus } from './modal-focus';
  import ParameterLabel from './ParameterLabel.svelte';

  let {
    sessionId,
    onclose,
    onsaved
  }: {
    sessionId: string;
    onclose: () => void;
    onsaved: () => Promise<unknown>;
  } = $props();
  const controls = [
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
      label: 'Unfinished-sentence pause (ms)',
      fallback: 3000,
      min: 0
    },
    {
      key: 'speech_block_max_internal_gap_ms',
      label: 'Maximum silence inside a block (ms)',
      fallback: 4000,
      min: 0
    }
  ];
  let stored = $state<SettingsPayload | null>(null);
  let values = $state<Record<string, number>>({});
  let earlyRepairEnabled = $state(false);
  let saving = $state(false);
  let error = $state('');
  let alive = true;
  const valid = $derived(
    controls.every(
      ({ key, min }) => Number.isInteger(values[key]) && values[key] >= min
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
          controls.map(({ key, fallback }) => [
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
  async function save() {
    if (!stored || !valid || saving) return;
    saving = true;
    error = '';
    try {
      await sessionApi.saveSettings(sessionId, 'tts', stored.revision, {
        ...stored.override,
        ...values,
        speech_block_early_repair_enabled: earlyRepairEnabled
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

<div
  class="fixed inset-0 z-[60] grid place-items-center bg-black/35 p-5 backdrop-blur-sm"
  role="presentation"
>
  <div
    use:modalFocus={{
      onclose: () => {
        if (!saving) onclose();
      }
    }}
    role="dialog"
    aria-modal="true"
    aria-labelledby="plan-settings-title"
    class="surface flex max-h-[90vh] w-full max-w-xl flex-col overflow-hidden rounded-3xl"
  >
    <div class="modal-scroll p-6 sm:p-7">
      <header class="flex items-start justify-between gap-4">
        <div>
          <h2 id="plan-settings-title" class="text-xl font-semibold">
            Speech-block settings
          </h2>
          <p class="muted mt-2 text-sm">
            Control how subtitle text becomes speech blocks. Block sizes apply
            when you prepare a new plan; saved versions remain available.
          </p>
        </div>
        <button
          class="btn btn-icon"
          aria-label="Close speech-block settings"
          disabled={saving}
          onclick={onclose}><X size={18} /></button
        >
      </header>
      {#if error}<p class="mt-4 text-sm text-red-600" role="alert">
          {error}
        </p>{/if}
      {#if !stored && !error}<p class="muted mt-4" role="status">
          Loading settings…
        </p>{/if}
      <form
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
        </fieldset>
        <footer
          class="mt-6 flex justify-end gap-2 border-t border-[var(--line)] pt-4"
        >
          <button
            type="button"
            class="btn btn-secondary"
            disabled={saving}
            onclick={onclose}>Cancel</button
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
      </form>
    </div>
  </div>
</div>
