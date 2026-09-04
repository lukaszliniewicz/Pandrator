<script lang="ts">
  import { Maximize2, Minimize2 } from '@lucide/svelte';

  let { maximized, ontoggle }: { maximized: boolean; ontoggle: () => void } =
    $props();

  const label = $derived(
    maximized ? 'Restore workspace size' : 'Maximize workspace'
  );
</script>

<button
  type="button"
  onclick={ontoggle}
  class="rounded-xl border border-[var(--line)] p-2"
  aria-label={label}
  aria-pressed={maximized}
  title={label}
>
  {#if maximized}<Minimize2 size={18} />{:else}<Maximize2 size={18} />{/if}
</button>
