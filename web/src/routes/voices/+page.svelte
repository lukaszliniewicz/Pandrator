<script lang="ts">
  import VoiceCatalogLibrary from '$lib/VoiceCatalogLibrary.svelte';
</script>

<!-- Let the library grow with its content so the page has one predictable
     scrollbar instead of nested panes competing for wheel and touch input. -->
<div class="min-h-[30rem]">
  <VoiceCatalogLibrary />
</div>
