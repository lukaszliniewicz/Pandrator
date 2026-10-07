<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import { ApiMutationAttempt } from './api';
  import { errorMessage } from './errors';
  import { modalFocus } from './modal-focus';
  import {
    previewRecast,
    startRecast,
    type RecastPreview
  } from './generation-recast';
  import type { GenerationRun } from './api-models';
  let {
    sessionId,
    initialKeys = null,
    onclose,
    onstarted
  }: {
    sessionId: string;
    initialKeys?: string[] | null;
    onclose: () => void;
    onstarted: (run: GenerationRun) => void;
  } = $props();
  let preview = $state<RecastPreview | null>(null);
  let selectedKeys = $state<string[]>([]);
  let force = $state(false);
  let includeMissing = $state(false);
  let loading = $state(true);
  let sending = $state(false);
  let error = $state('');
  let controller: AbortController | undefined;
  const attempt = new ApiMutationAttempt();
  onMount(() => {
    void refresh(initialKeys);
  });
  onDestroy(() => controller?.abort());
  async function refresh(keys: string[] | null = selectedKeys) {
    controller?.abort();
    const request = new AbortController();
    controller = request;
    loading = true;
    error = '';
    try {
      const result = await previewRecast(
        sessionId,
        { selector_keys: keys, force, include_missing: includeMissing },
        request.signal
      );
      if (request.signal.aborted) return;
      preview = result;
      selectedKeys = result.selected_keys;
    } catch (caught) {
      if (!request.signal.aborted) error = errorMessage(caught);
    } finally {
      if (!request.signal.aborted) loading = false;
    }
  }
  function toggle(key: string, checked: boolean) {
    selectedKeys = checked
      ? [...selectedKeys, key]
      : selectedKeys.filter((item) => item !== key);
    void refresh();
  }
  async function confirm() {
    if (
      !preview ||
      loading ||
      sending ||
      preview.blocked_reason ||
      !preview.generate_count
    )
      return;
    sending = true;
    error = '';
    try {
      const run = await startRecast(
        sessionId,
        {
          selector_keys: selectedKeys,
          force,
          include_missing: includeMissing,
          speech_plan_revision_id: preview.speech_plan_revision_id,
          expected_selection_hash: preview.selection_hash
        },
        attempt
      );
      onstarted(run);
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      sending = false;
    }
  }
</script>

<div class="fixed inset-0 z-[90] grid place-items-center bg-black/45 p-4">
  <div
    role="dialog"
    aria-modal="true"
    aria-labelledby="recast-title"
    use:modalFocus={{
      onclose: () => {
        if (!sending) onclose();
      }
    }}
    class="surface w-full max-w-xl max-h-[85vh] overflow-auto rounded-2xl p-5"
  >
    <h2 id="recast-title" class="text-lg font-semibold">
      Regenerate selected voices
    </h2>
    <p class="muted mt-2 text-sm">
      Keep the recordings you like and replace only the selected voices. Shared
      segments are rebuilt with the other speakers’ recorded assignments.
    </p>
    {#if preview}
      <p class="mt-3 text-sm">
        <strong>Current project renderer:</strong>
        {[
          preview.settings_summary.service,
          preview.settings_summary.model,
          preview.settings_summary.language
        ]
          .filter(Boolean)
          .join(' · ')}
      </p>
      <fieldset disabled={sending} class="mt-4 space-y-2">
        <legend class="text-sm font-semibold mb-2">Voices to regenerate</legend>
        {#each preview.selectors.filter((item) => item.affected_count || item.missing_count) as item (item.key)}
          <label
            class="flex items-start gap-3 rounded-xl border border-[var(--line)] p-3"
          >
            <input
              type="checkbox"
              class="mt-1"
              checked={selectedKeys.includes(item.key)}
              onchange={(event) =>
                toggle(item.key, event.currentTarget.checked)}
            />
            <span class="min-w-0"
              ><span class="font-semibold">{item.label}</span>
              <span class="muted block text-xs"
                >{item.changed_count} changed · {item.affected_count}
                {item.affected_count === 1
                  ? 'segment'
                  : 'segments'}{#if item.unverified_count}
                  · {item.unverified_count} unverified{/if}{#if item.overridden_count}
                  · {item.overridden_count} use an explicit override{/if}</span
              >
              {#if item.error}<span class="block text-xs text-red-700"
                  >{item.error}</span
                >{/if}
            </span>
          </label>
        {/each}
        <label class="flex gap-2 text-sm pt-2"
          ><input
            type="checkbox"
            checked={force}
            onchange={(event) => {
              force = event.currentTarget.checked;
              void refresh();
            }}
          />Regenerate selected voices even if unchanged or unverified</label
        >
        <label class="flex gap-2 text-sm"
          ><input
            type="checkbox"
            checked={includeMissing}
            onchange={(event) => {
              includeMissing = event.currentTarget.checked;
              void refresh();
            }}
          />Also generate missing recordings for selected voices</label
        >
      </fieldset>
      <div
        aria-live="polite"
        class="mt-4 rounded-xl bg-[var(--surface-soft)] p-3 text-sm"
      >
        {#if loading}Updating preview…{:else}<strong
            >{preview.generate_count}
            {preview.generate_count === 1 ? 'segment' : 'segments'} to generate</strong
          >
          · {preview.keep_count} retained
          {#if preview.shared_segment_count}<span
              class="muted block text-xs mt-1"
              >{preview.shared_segment_count} shared {preview.shared_segment_count ===
              1
                ? 'segment'
                : 'segments'}. Overlapping selections are counted once.</span
            >{/if}
          {#if preview.blocked_reason}<p role="alert" class="mt-2 text-red-700">
              {preview.blocked_reason}
            </p>{/if}
        {/if}
      </div>
      <p class="muted mt-3 text-xs">
        Old audio stays selected until each replacement succeeds. Both versions
        remain in history. Undo restores audio choices while keeping your saved
        cast.
      </p>
    {:else if loading}<p role="status" class="muted mt-4">
        Checking the current recordings…
      </p>{/if}
    {#if error}<p role="alert" class="mt-3 text-sm text-red-700">{error}</p>
      <button
        class="btn mt-2"
        disabled={sending}
        onclick={() => void refresh(preview ? selectedKeys : initialKeys)}
        >Refresh preview</button
      >{/if}
    <div class="mt-5 flex flex-wrap justify-end gap-2">
      <button class="btn" disabled={sending} onclick={onclose}>Cancel</button>
      <button
        class="btn btn-primary"
        disabled={sending ||
          loading ||
          !preview?.generate_count ||
          Boolean(preview?.blocked_reason)}
        onclick={() => void confirm()}
      >
        {sending
          ? 'Starting…'
          : `Regenerate ${preview?.generate_count ?? 0} ${preview?.generate_count === 1 ? 'segment' : 'segments'}`}
      </button>
    </div>
  </div>
</div>
