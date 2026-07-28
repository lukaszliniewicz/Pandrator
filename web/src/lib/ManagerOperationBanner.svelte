<script lang="ts">
  import { CircleAlert, LoaderCircle, X } from '@lucide/svelte';
  import { onMount } from 'svelte';
  import { managerApi } from './admin-api';

  type Operation = {
    id: string;
    state: string;
    progress: number;
    current_task_id?: string | null;
    error_message?: string | null;
  };

  type Status = {
    available: boolean;
    status?: { active_operation_id?: string | null };
  };

  const terminalStates = new Set([
    'succeeded',
    'failed',
    'cancelled',
    'recovery_required'
  ]);

  let operation = $state<Operation | null>(null);
  let error = $state('');
  let cancelling = $state(false);
  let dismissedOperation = $state('');
  let stopped = false;
  let timer: ReturnType<typeof setTimeout> | null = null;

  async function refresh() {
    if (timer) {
      clearTimeout(timer);
      timer = null;
    }
    let nextDelay = 5000;
    try {
      const status = await managerApi.status<Status>();
      if (!status.available) {
        operation = null;
        error = '';
        return;
      }
      const operationId =
        status.status?.active_operation_id
        ?? localStorage.getItem('pandrator-manager-operation');
      if (!operationId) {
        operation = null;
        error = '';
        return;
      }
      const current = await managerApi.operation<Operation>(operationId);
      if (!terminalStates.has(current.state)) {
        localStorage.setItem('pandrator-manager-operation', current.id);
        dismissedOperation = '';
        nextDelay = 1000;
      } else {
        localStorage.removeItem('pandrator-manager-operation');
        nextDelay = 5000;
      }
      operation = current.id === dismissedOperation ? null : current;
      error = '';
    } catch (caught) {
      error = caught instanceof Error ? caught.message : String(caught);
      nextDelay = 5000;
    } finally {
      if (!stopped) timer = setTimeout(() => void refresh(), nextDelay);
    }
  }

  async function cancel() {
    if (!operation || terminalStates.has(operation.state)) return;
    cancelling = true;
    try {
      await managerApi.cancel(operation.id);
      await refresh();
    } catch (caught) {
      error = caught instanceof Error ? caught.message : String(caught);
    } finally {
      cancelling = false;
    }
  }

  function dismiss() {
    if (!operation) return;
    dismissedOperation = operation.id;
    operation = null;
  }

  onMount(() => {
    void refresh();
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
    };
  });
</script>

{#if operation}
  {@const terminal = terminalStates.has(operation.state)}
  <aside
    class="mb-5 rounded-2xl border border-[var(--accent)] bg-[var(--accent-soft)] p-4"
    aria-live="polite"
  >
    <div class="flex flex-wrap items-center gap-3">
      {#if terminal && operation.state !== 'succeeded'}
        <CircleAlert class="shrink-0 text-amber-600" size={18}/>
      {:else}
        <LoaderCircle class={`shrink-0 ${!terminal ? 'animate-spin' : ''}`} size={18}/>
      {/if}
      <div class="min-w-0 flex-1">
        <div class="text-sm font-semibold">
          {terminal ? `Manager operation ${operation.state.replaceAll('_', ' ')}` : 'Local component operation in progress'}
        </div>
        <div class="muted mt-1 truncate text-xs">
          {operation.current_task_id ?? operation.error_message ?? operation.id}
        </div>
      </div>
      <a class="btn btn-sm btn-secondary" href="/providers?tab=local">View details</a>
      {#if !terminal}
        <button class="btn btn-sm btn-secondary" disabled={cancelling} onclick={cancel}>
          Cancel safely
        </button>
      {:else}
        <button class="btn btn-icon btn-secondary" aria-label="Dismiss operation status" onclick={dismiss}>
          <X size={16}/>
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
