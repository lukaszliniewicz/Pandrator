<script lang="ts">
  let {
    settingsStale = 0,
    identityUnknown = 0
  }: {
    settingsStale?: number;
    identityUnknown?: number;
  } = $props();
</script>

{#if settingsStale || identityUnknown}
  <div class="rounded-xl border border-amber-500/40 p-3 text-sm" role="status">
    <p class="font-semibold">
      Audio needs regeneration; the speech plan is preserved.
    </p>
    {#if settingsStale}
      <p class="mt-1">
        {settingsStale}
        {settingsStale === 1 ? 'block has' : 'blocks have'} audio from different generation
        settings or an older voice reference.
      </p>
    {/if}
    {#if identityUnknown}
      <p class="mt-1">
        {identityUnknown}
        {identityUnknown === 1 ? 'older take lacks' : 'older takes lack'}
        the settings evidence needed for automatic reuse.
      </p>
    {/if}
    <p class="mt-1">
      Use “Generate missing / stale only” to replace affected audio. Existing
      takes remain available for playback and selection.
    </p>
  </div>
{/if}
