<script lang="ts">
  import type { SpeechPlanState } from './session-flow';
  let {
    plan,
    disabled = false,
    label = 'Generate from speech plan',
    onselect
  }: {
    plan: SpeechPlanState | null;
    disabled?: boolean;
    label?: string;
    onselect: (id: string) => Promise<unknown>;
  } = $props();
</script>

<label class="block text-sm font-semibold"
  >{label}
  <select
    class="mt-2 w-full cursor-pointer rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 text-sm font-medium shadow-sm focus:border-[var(--accent)] focus:outline-none focus:ring-2 focus:ring-[var(--accent-soft)] disabled:cursor-not-allowed disabled:opacity-50"
    value={plan?.selected_revision_id ?? ''}
    disabled={disabled || !plan?.items.length}
    onchange={(event) => void onselect(event.currentTarget.value)}
  >
    {#if !plan?.items.length}<option value=""
        >Prepare a speech plan first</option
      >{/if}
    {#each plan?.items ?? [] as item (item.id)}
      <option value={item.id}
        >v{item.revision_number} · {item.reviewed
          ? 'Reviewed'
          : item.origin === 'manual'
            ? 'Edited'
            : 'Automatic'} · {item.active_segment_count} blocks{item.id ===
        plan?.latest_revision_id
          ? ' · latest'
          : ''}{!item.compatible ? ' · different text' : ''}</option
      >
    {/each}
  </select>
</label>
