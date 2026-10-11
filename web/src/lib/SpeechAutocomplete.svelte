<script lang="ts">
  let {
    label,
    value,
    choices,
    disabled = false,
    loading = false,
    onquery,
    oncreate,
    onchange
  }: {
    label: string;
    value: string;
    choices: Array<{ value: string; label: string; searchText?: string }>;
    disabled?: boolean;
    loading?: boolean;
    onquery?: (query: string) => void;
    oncreate?: (name: string) => void;
    onchange: (value: string) => void;
  } = $props();
  const id = $props.id();
  let query = $state('');
  let open = $state(false);
  let active = $state(-1);
  const selected = $derived(
    choices.find((entry) => entry.value === value)?.label ?? value
  );
  const matches = $derived(
    choices
      .filter((entry) =>
        `${entry.label} ${entry.value} ${entry.searchText ?? ''}`
          .toLowerCase()
          .includes(query.toLowerCase())
      )
      .slice(0, 50)
  );
  const canCreate = $derived(
    Boolean(
      oncreate &&
      query.trim() &&
      !choices.some(
        (entry) => entry.label.toLowerCase() === query.trim().toLowerCase()
      )
    )
  );
  function create() {
    oncreate?.(query.trim());
    open = false;
    query = '';
    active = -1;
  }
  function choose(next: string) {
    onchange(next);
    open = false;
    query = '';
    active = -1;
  }
</script>

<label class="choice-label" for={id}>{label}</label>
<div
  class="speech-choice"
  onfocusout={(event) => {
    if (
      !(event.relatedTarget instanceof Node) ||
      !event.currentTarget.contains(event.relatedTarget)
    )
      open = false;
  }}
>
  <input
    {id}
    class="input w-full"
    role="combobox"
    autocomplete="off"
    aria-expanded={open}
    aria-controls={`${id}-options`}
    aria-autocomplete="list"
    aria-activedescendant={open && active >= 0
      ? `${id}-option-${active}`
      : undefined}
    {disabled}
    value={open ? query : selected}
    placeholder={open ? 'Type to find a choice…' : 'Choose…'}
    onfocus={() => {
      query = '';
      open = true;
      active = -1;
      onquery?.('');
    }}
    oninput={(event) => {
      query = event.currentTarget.value;
      active = -1;
      open = true;
      onquery?.(query);
    }}
    onkeydown={(event) => {
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        open = true;
        active = !matches.length
          ? -1
          : active < 0
            ? event.key === 'ArrowDown'
              ? 0
              : matches.length - 1
            : (active + (event.key === 'ArrowDown' ? 1 : -1) + matches.length) %
              matches.length;
      } else if (event.key === 'Enter') {
        event.preventDefault();
        const choice =
          matches[active] ??
          matches.find(
            (entry) =>
              entry.value === query ||
              entry.label.toLowerCase() === query.toLowerCase()
          );
        if (choice) choose(choice.value);
        else if (!matches.length && canCreate) create();
      } else if (event.key === 'Escape' && open) {
        event.preventDefault();
        event.stopPropagation();
        open = false;
      }
    }}
  />
  {#if open}
    <ul
      id={`${id}-options`}
      role="listbox"
      aria-label={label}
      class="choice-options"
    >
      {#each matches as choice, index}
        <li
          id={`${id}-option-${index}`}
          role="option"
          aria-selected={value === choice.value}
          tabindex="-1"
          class:active={index === active}
          onpointerdown={(event) => event.preventDefault()}
          onpointermove={() => (active = index)}
          onkeydown={(event) => {
            if (event.key === 'Enter' || event.key === ' ') {
              event.preventDefault();
              choose(choice.value);
            }
          }}
          onclick={() => choose(choice.value)}
        >
          <span>{choice.label}</span>{#if value === choice.value}<span
              aria-hidden="true">✓</span
            >{/if}
        </li>
      {/each}
      {#if loading}<li class="choice-message" role="presentation">
          Searching…
        </li>
      {:else if !matches.length}<li class="choice-message" role="presentation">
          No matching choices
        </li>{/if}
      {#if canCreate}<li role="presentation">
          <button
            type="button"
            class="create-choice"
            onpointerdown={(event) => event.preventDefault()}
            onclick={create}>Add “{query.trim()}” as a speaker</button
          >
        </li>{/if}
    </ul>
  {/if}
</div>

<style>
  .choice-label {
    display: block;
    margin-bottom: 0.3rem;
    font-size: 0.83rem;
    font-weight: 600;
  }
  .speech-choice {
    position: relative;
  }
  .choice-options {
    position: absolute;
    inset: calc(100% + 0.2rem) 0 auto;
    z-index: 4;
    max-height: 13rem;
    overflow: auto;
    padding: 0.3rem;
    border: 1px solid var(--line);
    border-radius: 0.6rem;
    background: var(--paper-strong);
    box-shadow: var(--shadow);
    color: var(--ink);
    font-size: 0.8rem;
  }
  .choice-options li[role='option'] {
    display: flex;
    justify-content: space-between;
    gap: 0.5rem;
    padding: 0.45rem 0.55rem;
    border-radius: 0.35rem;
    cursor: pointer;
  }
  .choice-options li.active,
  .choice-options li[role='option']:hover {
    background: var(--accent-soft);
  }
  .choice-message {
    padding: 0.45rem 0.55rem;
    color: var(--muted);
    font-size: 0.75rem;
  }
  .create-choice {
    width: 100%;
    padding: 0.5rem;
    text-align: left;
    color: var(--accent);
    border-top: 1px solid var(--line);
  }
</style>
