<script lang="ts">
  import type { RecastSelector } from './generation-recast';
  let {
    impact,
    dirty,
    disabled,
    onclick
  }: {
    impact?: RecastSelector;
    dirty: boolean;
    disabled: boolean;
    onclick: () => void;
  } = $props();
</script>

{#if impact && impact.affected_count > impact.missing_count}
  <div class="mt-2 flex flex-wrap items-center justify-between gap-2">
    <span class="muted text-xs"
      >{#if dirty}Save to compare this voice with its recordings{:else}{impact.changed_count}
        changed · {impact.affected_count - impact.missing_count} recorded {impact.affected_count -
          impact.missing_count ===
        1
          ? 'segment'
          : 'segments'}{#if impact.unverified_count}
          · {impact.unverified_count} unverified{/if}{/if}</span
    >
    <button
      type="button"
      class="btn text-xs"
      {disabled}
      {onclick}
      title="Review affected recordings before regenerating this voice. Shared segments keep other speakers’ recorded assignments."
    >
      {dirty ? 'Save & review regeneration…' : 'Regenerate this voice…'}
    </button>
  </div>
{/if}
