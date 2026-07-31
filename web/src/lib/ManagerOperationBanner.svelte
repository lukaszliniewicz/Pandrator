<script lang="ts">
  import { CircleAlert, LoaderCircle, X } from '@lucide/svelte';
  import { onMount } from 'svelte';
  import {
    MANAGER_TERMINAL_STATES,
    managerOperationStore
  } from './manager-operation-store.svelte';

  let cancelling = $state(false);
  let dismissedOperation = $state('');
  const trackedOperation = $derived(managerOperationStore.operation);
  const operation = $derived(
    trackedOperation?.id === dismissedOperation ? null : trackedOperation
  );
  const error = $derived(managerOperationStore.error);

  async function cancel() {
    if (!operation || MANAGER_TERMINAL_STATES.has(operation.state)) return;
    cancelling = true;
    try {
      await managerOperationStore.cancel();
    } finally {
      cancelling = false;
    }
  }

  function dismiss() {
    if (!operation) return;
    dismissedOperation = operation.id;
  }

  onMount(() => {
    return managerOperationStore.connect((current) => {
      if (current && !MANAGER_TERMINAL_STATES.has(current.state)) {
        dismissedOperation = '';
      }
    });
  });
</script>

{#if operation}
  {@const terminal = MANAGER_TERMINAL_STATES.has(operation.state)}
  <aside
    class="mb-5 rounded-2xl border border-[var(--accent)] bg-[var(--accent-soft)] p-4"
    aria-live="polite"
  >
    <div class="flex flex-wrap items-center gap-3">
      {#if terminal && operation.state !== 'succeeded'}
        <CircleAlert class="shrink-0 text-amber-600" size={18} />
      {:else}
        <LoaderCircle
          class={`shrink-0 ${!terminal ? 'animate-spin' : ''}`}
          size={18}
        />
      {/if}
      <div class="min-w-0 flex-1">
        <div class="text-sm font-semibold">
          {terminal
            ? `Manager operation ${operation.state.replaceAll('_', ' ')}`
            : 'Local component operation in progress'}
        </div>
        <div class="muted mt-1 truncate text-xs">
          {operation.current_task_id ?? operation.error_message ?? operation.id}
        </div>
      </div>
      <a class="btn btn-sm btn-secondary" href="/providers?tab=local"
        >View details</a
      >
      {#if !terminal}
        <button
          class="btn btn-sm btn-secondary"
          disabled={cancelling}
          onclick={cancel}
        >
          Cancel safely
        </button>
      {:else}
        <button
          class="btn btn-icon btn-secondary"
          aria-label="Dismiss operation status"
          onclick={dismiss}
        >
          <X size={16} />
        </button>
      {/if}
    </div>
    {#if !terminal}
      <div class="mt-3 h-1.5 overflow-hidden rounded-full bg-black/10">
        <div
          class="h-full rounded-full bg-[var(--accent)] transition-[width]"
          style={`width:${Math.round(operation.progress * 100)}%`}
        ></div>
      </div>
    {/if}
    {#if error}<p class="mt-2 text-xs text-red-600">{error}</p>{/if}
  </aside>
{/if}
