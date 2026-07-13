<script lang="ts">import VoiceManager from '$lib/VoiceManager.svelte';</script>
<!-- Keep the library inside the shell's available viewport.  Its panes own their
     scrolling, so the page must not grow a second scrollbar around them. -->
<div class="h-[calc(100dvh-11.5rem)] min-h-[30rem] md:h-[calc(100svh-8.75rem)]"><VoiceManager onback={()=>history.back()}/></div>
