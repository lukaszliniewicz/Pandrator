<script lang="ts">
  import { controlFields } from './speech-markup-editor';
  import { deliverySupport, type SpeechCapabilities } from './speech-support';
  let {
    delivery,
    capabilities,
    scope = 'segment',
    enabled = true
  }: {
    delivery: Record<string, unknown>;
    capabilities?: SpeechCapabilities;
    scope?: 'segment' | 'phrase' | 'unresolved';
    enabled?: boolean;
  } = $props();
</script>

{#each controlFields.filter((field) => Boolean(delivery[field])) as field}
  {@const value = String(delivery[field])}
  {@const support = deliverySupport(capabilities, field, value, scope, enabled)}
  <span
    class="direction-badge"
    data-support={support.status}
    title={`${field}: ${value}\n${support.description}`}
  >
    <span class="direction-value"
      >{field === 'instruction' ? 'Direction' : field}: {value}</span
    >
    <span class="direction-status"
      >{support.status === 'sent'
        ? 'sent'
        : support.status === 'hint'
          ? 'hint'
          : support.label.toLowerCase()}</span
    >
  </span>
{/each}

<style>
  .direction-badge {
    display: inline-flex;
    align-items: baseline;
    gap: 0.35em;
    max-width: min(29rem, 100%);
    padding: 0.14rem 0.4rem;
    border: 1px solid color-mix(in srgb, var(--accent) 23%, var(--line));
    border-radius: 0.35rem;
    background: var(--accent-soft);
    color: var(--ink);
    font-size: 0.68rem;
    line-height: 1.5;
    vertical-align: middle;
  }
  .direction-value {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .direction-status {
    flex: none;
    font-size: 0.61rem;
    opacity: 0.8;
  }
  .direction-badge[data-support='unsupported'],
  .direction-badge[data-support='unknown'] {
    border-style: dashed;
    background: color-mix(in srgb, var(--paper-strong) 85%, #b67d26);
  }
  .direction-badge[data-support='disabled'] {
    opacity: 0.65;
  }
</style>
