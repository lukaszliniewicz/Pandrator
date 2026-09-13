<script lang="ts">
  import { CircleHelp } from '@lucide/svelte';
  import { helpPopover } from './help-popover';
  import { onMount, onDestroy } from 'svelte';
  import {
    parameterDefinition,
    type ParameterDefinition
  } from './parameter-help';

  let {
    section,
    name,
    label,
    controlId,
    compact = false
  }: {
    section: string;
    name: string;
    label: string;
    controlId?: string;
    compact?: boolean;
  } = $props();
  const tooltipId = $props.id();
  let definition = $state<ParameterDefinition | null>(null);
  let trigger = $state<HTMLSpanElement>();
  let tooltip = $state<HTMLSpanElement>();
  const { show, hide, leave, escape } = helpPopover(
    () => trigger,
    () => tooltip
  );

  onDestroy(hide);

  const constraint = $derived.by(() => {
    if (!definition) return '';
    const parts: string[] = [];
    if (definition.choices?.length)
      parts.push(`Choices: ${definition.choices.map(String).join(', ')}`);
    if (definition.minimum != null || definition.maximum != null) {
      const unit = definition.unit ? ` ${definition.unit}` : '';
      if (definition.minimum != null && definition.maximum != null)
        parts.push(`Range: ${definition.minimum}–${definition.maximum}${unit}`);
      else if (definition.minimum != null)
        parts.push(`Minimum: ${definition.minimum}${unit}`);
      else parts.push(`Maximum: ${definition.maximum}${unit}`);
    }
    return parts.join(' · ');
  });

  onMount(() => {
    parameterDefinition(section, name).then((value) => (definition = value));
  });
</script>

{#if definition?.description}
  <span class="parameter-label-row">
    {#if controlId}<label for={controlId}>{label}</label>{:else}<span
        >{label}</span
      >{/if}<span
      role="button"
      bind:this={trigger}
      tabindex="0"
      class:compact
      class="parameter-help"
      aria-describedby={tooltipId}
      aria-label={`About ${label}`}
      onmouseenter={show}
      onmouseleave={leave}
      onfocus={show}
      onblur={hide}
      onclick={(event) => {
        event.preventDefault();
        event.stopPropagation();
        trigger?.focus();
        show();
      }}
      onkeydowncapture={escape}
      onkeydown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          event.stopPropagation();
          show();
        }
      }}
    >
      <CircleHelp size={compact ? 12 : 13} aria-hidden="true" />
      <span
        bind:this={tooltip}
        id={tooltipId}
        role="tooltip"
        popover="manual"
        class="parameter-tooltip"
      >
        <span>{definition.description}</span>
        {#if definition.applicability}<span class="tooltip-detail"
            >{definition.applicability}</span
          >{/if}
        {#if constraint}<span class="tooltip-detail">{constraint}</span>{/if}
        {#if definition.caveat}<span class="tooltip-caveat"
            >Note: {definition.caveat}</span
          >{/if}
      </span>
    </span>
  </span>
{:else if controlId}
  <label for={controlId}>{label}</label>
{:else}
  <span>{label}</span>
{/if}

<style>
  .parameter-label-row {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
  }
  .parameter-help {
    position: relative;
    display: inline-grid;
    place-items: center;
    cursor: help;
    border-radius: 0.35rem;
    outline: none;
    border: 0;
    background: transparent;
    padding: 0;
    color: inherit;
    font: inherit;
  }
  .parameter-help :global(svg) {
    flex: none;
    color: var(--muted);
  }
  .parameter-help:focus-visible {
    box-shadow: 0 0 0 2px var(--accent-soft);
  }
  .parameter-tooltip {
    position: fixed;
    margin: 0;
    inset: auto;
    width: max-content;
    max-width: min(22rem, 80vw);
    max-height: calc(100dvh - 16px);
    overflow-y: auto;
    border: 1px solid #4b4650;
    border-radius: 0.65rem;
    background: #242126;
    padding: 0.65rem 0.75rem;
    color: #fff;
    font-size: 0.72rem;
    font-weight: 500;
    line-height: 1.45;
    text-align: left;
    pointer-events: auto;
    box-shadow: 0 0.7rem 1.8rem rgb(0 0 0 / 24%);
  }
  .parameter-tooltip > span {
    display: block;
  }
  .tooltip-detail,
  .tooltip-caveat {
    margin-top: 0.35rem;
    color: #d8d2dc;
  }
  .tooltip-caveat {
    color: #f1d9ff;
  }
  .compact .parameter-tooltip {
    font-size: 0.68rem;
  }
</style>
