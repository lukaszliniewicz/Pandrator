<script lang="ts">
  import type { ProviderModelLifecycle } from './api-models';

  let { lifecycle }: { lifecycle: ProviderModelLifecycle | undefined } =
    $props();
  function dateLabel(value: string) {
    return new Date(`${value}T00:00:00Z`).toLocaleDateString('en-GB', {
      day: 'numeric',
      month: 'long',
      year: 'numeric',
      timeZone: 'UTC'
    });
  }
</script>

{#if lifecycle}
  <aside
    aria-label="Model lifecycle"
    class="mt-4 rounded-xl border border-amber-500/40 bg-amber-500/10 p-4 text-sm"
  >
    <p class="font-semibold">Provider has deprecated this model</p>
    <p class="mt-1">
      {#if lifecycle.shutdown_date}
        Scheduled shutdown: {dateLabel(lifecycle.shutdown_date)}.
      {:else}
        An exact shutdown date is not listed for this model.
      {/if}
      Saved project settings stay unchanged until you choose another model.
    </p>
    {#if lifecycle.replacement_model_ids.length}
      <p class="mt-2 break-words">
        Provider suggestions: {lifecycle.replacement_model_ids.join(', ')}.
        {#if lifecycle.migration_kind === 'different_api'}
          These use a different API; narration compatibility needs verification
          before migration.
        {:else}
          Choose a replacement explicitly using the Model field.
        {/if}
      </p>
    {/if}
    <a
      class="mt-2 inline-block font-semibold text-[var(--accent)]"
      href={lifecycle.source_url}
      target="_blank"
      rel="noreferrer external">Provider lifecycle information</a
    >
  </aside>
{/if}
