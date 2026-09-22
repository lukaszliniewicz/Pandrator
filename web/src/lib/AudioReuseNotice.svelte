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
      Some recordings use earlier or unverified settings.
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
        {identityUnknown === 1 ? 'older take was' : 'older takes were'} made with
        earlier settings, or {identityUnknown === 1 ? 'its' : 'their'} saved settings
        could not be verified.
      </p>
    {/if}
    <p class="mt-1">
      “Continue unfinished audio” deliberately keeps these recordings. Use
      “Refresh changed audio…” to regenerate them with the current voice and
      settings. Existing takes remain available for playback and selection.
    </p>
  </div>
{/if}
