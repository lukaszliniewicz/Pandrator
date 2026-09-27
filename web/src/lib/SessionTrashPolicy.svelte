<script lang="ts">
  import { onMount } from 'svelte';
  import { apiJson } from './api';
  import { errorMessage } from './errors';
  type Policy = { revision: number; days: number | null };
  let policy = $state<Policy | null>(null);
  let enabled = $state(false);
  let days = $state(30);
  let busy = $state(false);
  let error = $state('');
  let message = $state('');
  const dirty = $derived(
    policy &&
      (enabled !== (policy.days !== null) || (enabled && days !== policy.days))
  );
  async function load() {
    try {
      policy = await apiJson<Policy>('/session-trash-policy');
      enabled = policy.days !== null;
      days = policy.days ?? 30;
      error = '';
    } catch (caught) {
      error = errorMessage(caught);
    }
  }
  async function save() {
    if (!policy) return;
    busy = true;
    error = message = '';
    try {
      policy = await apiJson<Policy>('/session-trash-policy', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          expected_revision: policy.revision,
          days: enabled ? days : null
        })
      });
      message = enabled
        ? `Sessions moved to Trash from now on will be deleted after ${days} days.`
        : 'Automatic session deletion is off.';
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      busy = false;
    }
  }
  onMount(() => {
    void load();
  });
</script>

<section class="surface rounded-2xl p-5" aria-label="Session trash retention">
  <h2 class="text-lg font-semibold">Session Trash</h2>
  <p class="muted mt-2 text-sm">
    Keep deleted sessions for recovery, or choose when to remove them
    permanently.
  </p>
  {#if error}<p role="alert" class="mt-3 text-sm text-red-600">
      {error} <button class="underline" onclick={load}>Reload policy</button>
    </p>{/if}
  {#if policy}
    <fieldset disabled={busy} class="mt-4 space-y-3">
      <label class="flex items-center gap-2 text-sm"
        ><input type="checkbox" bind:checked={enabled} />Automatically delete
        sessions from Trash</label
      >
      {#if enabled}<label class="block text-sm"
          >Days in Trash<input
            class="input mt-1 block w-32"
            type="number"
            min="1"
            max="3650"
            step="1"
            bind:value={days}
          /></label
        >{/if}
      <p class="muted text-xs leading-relaxed">
        {enabled
          ? 'Deletion is permanent. This policy applies to future moves to Trash; existing entries keep their saved dates. Disabling it pauses scheduled deletion. Cleanup runs while Pandrator is open and catches up after startup. Cleanup that has already started will finish.'
          : 'Sessions stay recoverable until you choose Delete permanently. Previously scheduled deletion is paused. Cleanup that has already started will finish.'}
      </p>
      <button
        class="btn"
        onclick={save}
        disabled={!dirty ||
          (enabled && (!Number.isInteger(days) || days < 1 || days > 3650))}
        >{busy ? 'Saving…' : 'Save Trash policy'}</button
      >
    </fieldset>
  {:else if !error}<p class="muted mt-3 text-sm">Loading Trash policy…</p>{/if}
  {#if message}<p role="status" class="muted mt-3 text-sm">{message}</p>{/if}
</section>
