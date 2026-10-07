<script lang="ts">
  let { metadata }: { metadata: unknown } = $props();
  const count = $derived.by(() => {
    if (!metadata || typeof metadata !== 'object') return 0;
    const diagnostics = (metadata as Record<string, unknown>)
      .book_timing_diagnostics;
    if (!diagnostics || typeof diagnostics !== 'object') return 0;
    const value = Number(
      (diagnostics as Record<string, unknown>).original_mapping_fallback_count
    );
    return Number.isFinite(value) && value > 0 ? value : 0;
  });
</script>

{#if count}
  <p
    class="mt-3 rounded-xl border border-amber-500/30 bg-amber-500/10 p-3 text-sm"
    role="status"
  >
    {count}
    {count === 1 ? 'segment uses' : 'segments use'} spoken wording because the original
    text could not be mapped reliably to the audio. Whole-segment export retains complete
    original wording; smaller passages may need text review.
  </p>
{/if}
