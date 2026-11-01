<script lang="ts">
  import { ArrowLeft } from '@lucide/svelte';
  import AccountManager from '$lib/AccountManager.svelte';
</script>

<svelte:head><title>Users — Pandrator</title></svelte:head>

<div class="mx-auto max-w-5xl">
  <a
    class="muted inline-flex items-center gap-2 text-sm hover:underline"
    href="/settings"
  >
    <ArrowLeft size={15} /> Application settings
  </a>
  <AccountManager />
</div>
