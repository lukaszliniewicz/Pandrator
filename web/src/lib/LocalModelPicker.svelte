<script lang="ts">
  import { ChevronDown } from '@lucide/svelte';
  import {
    badgeLabelFor,
    filterLocalGroups,
    findLocalModel,
    groupLocalModels,
    resolveLocalModels,
    summarizeLocalModel,
    type LocalModelCatalogEntry
  } from './local-model-groups';

  let {
    value = '',
    catalog = [],
    listedIds = [],
    loadedIds = [],
    installedIds = [],
    modelVoiceModes = {},
    familyLabels = {},
    label = 'Model',
    placeholder = 'Choose a model',
    searchPlaceholder = 'Search models, variants, languages…',
    disabled = false,
    id = 'local-model-picker',
    onchange
  }: {
    value: string;
    catalog: LocalModelCatalogEntry[];
    listedIds?: string[];
    loadedIds?: string[];
    installedIds?: string[];
    modelVoiceModes?: Record<string, string>;
    familyLabels?: Record<string, string>;
    label?: string;
    placeholder?: string;
    searchPlaceholder?: string;
    disabled?: boolean;
    id?: string;
    onchange: (value: string) => void;
  } = $props();

  let open = $state(false);
  let query = $state('');
  let toggledGroups = $state<string[]>([]);
  let toggledSubgroups = $state<string[]>([]);
  let triggerEl = $state<HTMLButtonElement | null>(null);
  let searchEl = $state<HTMLInputElement | null>(null);
  let panelEl = $state<HTMLDivElement | null>(null);

  const panelId = $derived(`${id}-panel`);
  const popoverSupported = $derived(
    typeof HTMLElement !== 'undefined' && 'showPopover' in HTMLElement.prototype
  );

  const resolved = $derived(
    resolveLocalModels(catalog, listedIds, value ? [value] : [], {
      voiceModes: modelVoiceModes,
      loadedIds,
      installedIds,
      familyLabels
    })
  );
  const groups = $derived(groupLocalModels(resolved));
  const searching = $derived(Boolean(query.trim()));
  const visible = $derived(
    searching ? filterLocalGroups(groups, query) : groups
  );
  const selection = $derived(findLocalModel(groups, value));
  const summary = $derived(summarizeLocalModel(groups, value, placeholder));
  const matchCount = $derived(
    visible.reduce(
      (count, group) =>
        count +
        group.subgroups.reduce(
          (inner, subgroup) => inner + subgroup.models.length,
          0
        ),
      0
    )
  );

  function isGroupOpen(groupId: string): boolean {
    if (searching) return true;
    if (toggledGroups.includes(groupId)) return selection?.group.id !== groupId;
    return selection?.group.id === groupId;
  }

  function isSubgroupOpen(groupId: string, subgroupId: string): boolean {
    if (searching) return true;
    const key = `${groupId}/${subgroupId}`;
    if (toggledSubgroups.includes(key))
      return selection?.subgroup.id !== subgroupId;
    return selection?.subgroup.id === subgroupId;
  }

  function toggle(list: string[], key: string): string[] {
    return list.includes(key)
      ? list.filter((item) => item !== key)
      : [...list, key];
  }

  function choose(next: string) {
    // The only state change this picker ever makes: an explicit choice.
    // Expanding, searching, or opening never touches the selection.
    if (next !== value) onchange(next);
    open = false;
    query = '';
    triggerEl?.focus();
  }

  function onEscape(event: KeyboardEvent) {
    if (event.key === 'Escape') {
      event.stopPropagation();
      open = false;
      query = '';
      triggerEl?.focus();
    }
  }

  function onToggle(event: Event) {
    // Queued native events can arrive after a newer toggle or unmount. Read
    // the live element state, and do not steal focus from an outside click.
    const panel = event.currentTarget as HTMLDivElement | null;
    if (!panel?.isConnected || panel !== panelEl) return;
    open = panel.matches(':popover-open');
  }

  $effect(() => {
    const panel = panelEl;
    if (!panel) return;
    if (typeof panel.showPopover !== 'function') {
      // Fallback for browsers without the popover API: the panel stays in
      // normal flow (see hidden binding) with a manual outside close.
      if (!open) return;
      searchEl?.focus();
      const onPointer = (event: MouseEvent) => {
        const target = event.target as Node | null;
        if (
          target &&
          !panel.contains(target) &&
          triggerEl &&
          !triggerEl.contains(target)
        )
          open = false;
      };
      document.addEventListener('mousedown', onPointer);
      return () => document.removeEventListener('mousedown', onPointer);
    }
    // Native popover renders in the top layer, so ancestor overflow or a
    // modal stacking context can never clip the chooser.
    const showing = panel.matches(':popover-open');
    if (open && !showing) {
      panel.showPopover();
      searchEl?.focus();
    } else if (!open && showing) {
      panel.hidePopover();
    }
  });

  $effect(() => {
    const panel = panelEl;
    const trigger = triggerEl;
    if (!open || !panel || !trigger || typeof panel.showPopover !== 'function') return;
    const place = () => {
      const anchor = trigger.getBoundingClientRect();
      const width = panel.offsetWidth;
      const height = panel.offsetHeight;
      const viewportWidth = document.documentElement.clientWidth;
      const viewportHeight = window.innerHeight;
      const left = Math.max(16, Math.min(anchor.left, viewportWidth - width - 16));
      const below = anchor.bottom + 6;
      const top = below + height <= viewportHeight - 16
        ? below
        : Math.max(16, Math.min(anchor.top - height - 6, viewportHeight - height - 16));
      panel.style.left = `${left}px`;
      panel.style.top = `${top}px`;
    };
    const frame = requestAnimationFrame(place);
    const observer = new ResizeObserver(place);
    observer.observe(panel);
    observer.observe(trigger);
    window.addEventListener('resize', place);
    window.addEventListener('scroll', place, true);
    return () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
      window.removeEventListener('resize', place);
      window.removeEventListener('scroll', place, true);
    };
  });
</script>

<div class="local-model-picker min-w-0">
  <span class="picker-label">{label}</span>
  <button
    type="button"
    bind:this={triggerEl}
    class="picker-trigger"
    aria-haspopup="dialog"
    aria-expanded={open}
    aria-controls={panelId}
    aria-label={`${label}: ${summary}`}
    {disabled}
    onclick={() => {
      open = !open;
    }}
  >
    <span class="min-w-0 flex-1 text-left">
      <span class="block truncate font-normal">{summary}</span>
      {#if selection}<span class="muted block truncate text-xs">
          {selection.model.id}
          {#if selection.model.badge !== 'unknown'}
            · {badgeLabelFor(selection.model.badge)}{/if}
        </span>{/if}
    </span>
    {#if selection}<span
        class="status-chip shrink-0"
        data-badge={selection.model.badge}
        title={selection.model.badgeReason ||
          `Model state: ${badgeLabelFor(selection.model.badge)}`}
        >{badgeLabelFor(selection.model.badge)}</span
      >{/if}
    <ChevronDown size={16} class="muted shrink-0" />
  </button>

  {#if open || popoverSupported}
    <div
      bind:this={panelEl}
      id={panelId}
      popover={popoverSupported ? 'auto' : undefined}
      tabindex="-1"
      hidden={!popoverSupported && !open}
      class="picker-panel"
      role="dialog"
      aria-label={label}
      onkeydown={onEscape}
      ontoggle={onToggle}
    >
      <input
        bind:this={searchEl}
        bind:value={query}
        type="search"
        class="picker-search"
        placeholder={searchPlaceholder}
        aria-label="Search models"
      />
      <div class="picker-count muted" role="status">
        {matchCount} of {resolved.length} models{#if searching}
          match{/if}
      </div>
      <div class="picker-list">
        {#each visible as group (group.id)}
          <section class="picker-group">
            <button
              type="button"
              class="picker-group-toggle"
              aria-expanded={isGroupOpen(group.id)}
              onclick={() => {
                toggledGroups = toggle(toggledGroups, group.id);
              }}
            >
              <ChevronDown
                size={14}
                class={isGroupOpen(group.id) ? 'rotate-180' : ''}
              />
              <span class="min-w-0 flex-1 truncate text-left"
                >{group.label}</span
              >
              <span class="muted text-xs">{group.total}</span>
            </button>
            {#if isGroupOpen(group.id)}
              {#each group.subgroups as subgroup (subgroup.id)}
                <div class="picker-subgroup">
                  {#if subgroup.label}
                    <button
                      type="button"
                      class="picker-subgroup-toggle"
                      aria-expanded={isSubgroupOpen(group.id, subgroup.id)}
                      onclick={() => {
                        toggledSubgroups = toggle(
                          toggledSubgroups,
                          `${group.id}/${subgroup.id}`
                        );
                      }}
                    >
                      <ChevronDown
                        size={13}
                        class={isSubgroupOpen(group.id, subgroup.id)
                          ? 'rotate-180'
                          : ''}
                      />
                      <span class="min-w-0 flex-1 truncate text-left"
                        >{subgroup.label}</span
                      >
                      {#if subgroup.detail}<span class="muted truncate text-xs"
                          >{subgroup.detail}</span
                        >{/if}
                    </button>
                  {/if}
                  {#if !subgroup.label || isSubgroupOpen(group.id, subgroup.id)}
                    <fieldset class="picker-options">
                      {#if subgroup.label}<legend class="sr-only"
                          >{group.label} · {subgroup.label}</legend
                        >{/if}
                      {#each subgroup.models as model (model.id)}
                        <label
                          class="picker-option"
                          title={model.description ||
                            model.recommendedFor ||
                            model.label}
                        >
                          <input
                            type="radio"
                            name={id}
                            value={model.id}
                            checked={model.id === value}
                            {disabled}
                            onchange={() => choose(model.id)}
                          />
                          <span class="min-w-0 flex-1">
                            <span class="flex flex-wrap items-center gap-1.5">
                              <strong class="font-semibold"
                                >{model.precisionLabel || model.label}</strong
                              >
                              <span
                                class="status-chip"
                                data-badge={model.badge}
                                title={model.badgeReason ||
                                  `Model state: ${badgeLabelFor(model.badge)}`}
                                >{badgeLabelFor(model.badge)}</span
                              >
                              {#if model.recommendedFor}<span
                                  class="recommend-chip"
                                  title={model.recommendedFor}>Recommended</span
                                >{/if}
                              {#if model.custom}<span class="muted text-xs"
                                  >Custom</span
                                >{/if}
                            </span>
                            {#if (model.precisionLabel && model.label !== model.precisionLabel) || (!model.precisionLabel && model.label !== model.id)}
                              <span class="muted block truncate text-xs"
                                >{model.label}</span
                              >
                            {/if}
                            <span
                              class="muted block truncate font-mono text-[.68rem]"
                              >{model.id}</span
                            >
                          </span>
                        </label>
                      {/each}
                    </fieldset>
                  {/if}
                </div>
              {/each}
            {/if}
          </section>
        {:else}
          <p class="muted picker-empty">
            No models match.
            {#if value}The current selection is kept unchanged.{/if}
          </p>
        {/each}
      </div>
    </div>
  {/if}
</div>

<style>
  .local-model-picker {
    position: relative;
    display: block;
  }
  .picker-label {
    display: block;
    font-size: 0.75rem;
    font-weight: 600;
  }
  .picker-trigger {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin-top: 0.4rem;
    width: 100%;
    border: 1px solid var(--line);
    border-radius: 0.72rem;
    background: var(--paper);
    color: var(--ink);
    padding: 0.65rem 0.72rem;
    font-size: 0.85rem;
  }
  .picker-trigger:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }
  .picker-panel {
    width: min(30rem, calc(100vw - 2rem));
    max-height: min(24rem, calc(100vh - 4rem));
    overflow: auto;
    border: 1px solid var(--line);
    border-radius: 0.9rem;
    background: var(--paper);
    color: var(--ink);
    box-shadow: 0 1.2rem 2.5rem rgb(0 0 0 / 0.18);
    padding: 0.6rem;
    margin: 0;
  }
  /* The top layer escapes ancestor clipping. Placement follows the trigger
     and is clamped to the viewport, including after resize or scrolling. */
  .picker-panel[popover] {
    position: fixed;
    inset: 1rem auto auto 1rem;
  }
  /* Non-popover fallback keeps the dropdown in normal flow. */
  div.picker-panel:not([popover]) {
    position: absolute;
    z-index: 40;
    width: 100%;
    min-width: 18rem;
    max-height: 24rem;
  }
  .picker-search {
    width: 100%;
    border: 1px solid var(--line);
    border-radius: 0.65rem;
    background: var(--paper);
    color: var(--ink);
    padding: 0.55rem 0.65rem;
    font-size: 0.82rem;
  }
  .picker-count {
    margin: 0.45rem 0.25rem 0.1rem;
    font-size: 0.7rem;
  }
  .picker-list {
    display: grid;
    gap: 0.35rem;
    margin-top: 0.35rem;
  }
  .picker-group {
    border: 1px solid var(--line);
    border-radius: 0.7rem;
    padding: 0.25rem;
  }
  .picker-group-toggle,
  .picker-subgroup-toggle {
    display: flex;
    align-items: center;
    gap: 0.45rem;
    width: 100%;
    border-radius: 0.55rem;
    padding: 0.5rem 0.55rem;
    font-size: 0.8rem;
    font-weight: 700;
  }
  .picker-group-toggle:hover,
  .picker-subgroup-toggle:hover {
    background: var(--accent-soft);
  }
  .picker-subgroup {
    margin: 0.15rem 0.15rem 0.3rem 0.9rem;
  }
  .picker-subgroup-toggle {
    font-size: 0.76rem;
  }
  .picker-options {
    display: grid;
    gap: 0.2rem;
    margin: 0.2rem 0 0.3rem;
    border: 0;
    padding: 0;
  }
  .picker-option {
    display: flex;
    gap: 0.55rem;
    align-items: flex-start;
    border-radius: 0.55rem;
    padding: 0.45rem 0.55rem;
    font-size: 0.78rem;
    cursor: pointer;
  }
  .picker-option:hover {
    background: var(--accent-soft);
  }
  .picker-option input {
    margin-top: 0.2rem;
    accent-color: var(--accent);
  }
  .status-chip {
    border: 1px solid var(--line);
    border-radius: 999px;
    padding: 0.05rem 0.45rem;
    font-size: 0.64rem;
    font-weight: 700;
    white-space: nowrap;
  }
  .status-chip[data-badge='loaded'] {
    border-color: color-mix(in srgb, var(--success) 55%, var(--line));
    color: var(--success);
  }
  .status-chip[data-badge='installed'] {
    border-color: color-mix(in srgb, var(--accent) 55%, var(--line));
    color: var(--accent);
  }
  .status-chip[data-badge='unknown'] {
    opacity: 0.75;
  }
  .recommend-chip {
    border-radius: 999px;
    background: var(--accent-soft);
    color: var(--accent);
    padding: 0.05rem 0.45rem;
    font-size: 0.64rem;
    font-weight: 700;
    white-space: nowrap;
  }
  .picker-empty {
    padding: 0.8rem 0.4rem;
    font-size: 0.8rem;
  }
  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0 0 0 0);
  }
</style>
