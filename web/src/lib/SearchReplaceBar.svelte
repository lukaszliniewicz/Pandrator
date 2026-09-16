<script lang="ts">
  import { errorMessage } from './errors';
  import {
    CaseSensitive,
    ChevronDown,
    ChevronUp,
    LoaderCircle,
    Search,
    WholeWord
  } from '@lucide/svelte';
  import { tick, untrack } from 'svelte';
  import {
    findTextMatches,
    replacementsForMatches,
    type TextReplacement,
    type TextSearchMatch
  } from './search-replace';

  let {
    texts,
    onreplace,
    onnavigate,
    onactivate,
    disabled = false,
    label = 'editable text',
    onsearch,
    searching = false,
    serverMatches,
    showScopeSelector = false,
    searchScope = 'cue',
    onScopeChange,
    onclose,
    autoFocus = false
  }: {
    texts: string[];
    onreplace: (updates: TextReplacement[]) => void | Promise<void>;
    onnavigate?: (match: TextSearchMatch) => void | Promise<void>;
    onactivate?: () => void | Promise<void>;
    disabled?: boolean;
    label?: string;
    onsearch?: (
      query: string,
      options: { matchCase: boolean; wholeWord: boolean }
    ) => void;
    searching?: boolean;
    serverMatches?: TextSearchMatch[];
    showScopeSelector?: boolean;
    searchScope?: 'cue' | 'tts';
    onScopeChange?: (scope: 'cue' | 'tts') => void;
    onclose?: () => void;
    autoFocus?: boolean;
  } = $props();

  let query = $state('');
  let replacement = $state('');
  let matchCase = $state(false);
  let wholeWord = $state(false);
  let activeIndex = $state(0);
  let replacing = $state(false);
  let error = $state('');
  let findInput: HTMLInputElement | undefined = $state();
  let focusedOnMount = false;

  // Focus the find field once when the panel mounts (it remounts on open).
  $effect(() => {
    if (autoFocus && !focusedOnMount && findInput) {
      focusedOnMount = true;
      findInput.focus();
    }
  });

  // A scope switch must not carry a stale match selection: the match list,
  // counter, and navigation all re-resolve against the newly selected field.
  $effect(() => {
    void searchScope;
    void showScopeSelector;
    untrack(() => {
      activeIndex = 0;
      error = '';
    });
  });

  const matches = $derived(
    serverMatches ?? findTextMatches(texts, query, { matchCase, wholeWord })
  );
  const currentIndex = $derived(
    matches.length ? Math.min(activeIndex, matches.length - 1) : 0
  );
  const currentMatch = $derived(matches[currentIndex]);

  $effect(() => {
    const search = { query, matchCase, wholeWord };
    untrack(() => onsearch?.(search.query, search));
  });

  function resetSearchPosition() {
    activeIndex = 0;
    error = '';
  }

  async function activate() {
    await onactivate?.();
  }

  async function navigate(step: number) {
    if (searching || !matches.length) return;
    activeIndex = (currentIndex + step + matches.length) % matches.length;
    await onnavigate?.(matches[activeIndex]);
  }

  function searchKeydown(event: KeyboardEvent) {
    if (event.key === 'Escape') {
      event.preventDefault();
      onclose?.();
      return;
    }
    if (event.key !== 'Enter') return;
    event.preventDefault();
    navigate(event.shiftKey ? -1 : 1);
  }

  function replaceKeydown(event: KeyboardEvent) {
    if (event.key === 'Escape') {
      event.preventDefault();
      onclose?.();
    }
  }

  async function apply(updates: TextReplacement[]) {
    if (!updates.length) return;
    replacing = true;
    error = '';
    try {
      await onreplace(updates);
      await tick();
      if (matches.length) {
        activeIndex = Math.min(currentIndex, matches.length - 1);
        await onnavigate?.(matches[activeIndex]);
      } else {
        activeIndex = 0;
      }
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      replacing = false;
    }
  }

  function replaceCurrent() {
    if (!currentMatch) return;
    apply(replacementsForMatches(texts, [currentMatch], replacement));
  }

  function replaceAll() {
    apply(replacementsForMatches(texts, matches, replacement));
  }
</script>

<div
  class="search-replace"
  role="search"
  aria-label={`Search and replace ${label}`}
>
  <div class="find-field">
    <Search class="search-icon" size={15} />
    <input
      bind:this={findInput}
      value={query}
      onfocus={activate}
      oninput={(event) => {
        query = event.currentTarget.value;
        resetSearchPosition();
      }}
      onkeydown={searchKeydown}
      placeholder="Find"
      aria-label={`Find in ${label}`}
    />
    <span class="count" aria-live="polite"
      >{searching
        ? 'Searching…'
        : query
          ? matches.length
            ? `${currentIndex + 1} / ${matches.length}`
            : 'No matches'
          : ''}</span
    >
    <button
      onclick={() => navigate(-1)}
      disabled={searching || !matches.length}
      title="Previous match"
      aria-label="Previous match"><ChevronUp size={15} /></button
    >
    <button
      onclick={() => navigate(1)}
      disabled={searching || !matches.length}
      title="Next match"
      aria-label="Next match"><ChevronDown size={15} /></button
    >
  </div>
  <div class="replace-field">
    <input
      bind:value={replacement}
      onfocus={activate}
      onkeydown={replaceKeydown}
      placeholder="Replace with"
      aria-label={`Replace in ${label}`}
    />
    <button
      onclick={replaceCurrent}
      disabled={disabled || searching || replacing || !currentMatch}
      >Replace</button
    >
    <button
      onclick={replaceAll}
      disabled={disabled || searching || replacing || !matches.length}
      >Replace all</button
    >
  </div>
  <div class="options">
    {#if showScopeSelector}
      <div class="scope-switch" role="radiogroup" aria-label="Search scope">
        <button
          type="button"
          role="radio"
          aria-checked={searchScope === 'cue'}
          class:active={searchScope === 'cue'}
          title="Search and replace cue text"
          aria-label="Cue text"
          onclick={() => onScopeChange?.('cue')}>Cue text</button
        >
        <button
          type="button"
          role="radio"
          aria-checked={searchScope === 'tts'}
          class:active={searchScope === 'tts'}
          title="Search and replace TTS text"
          aria-label="TTS text"
          onclick={() => onScopeChange?.('tts')}>TTS text</button
        >
      </div>
    {/if}
    <button
      onclick={() => {
        matchCase = !matchCase;
        resetSearchPosition();
      }}
      class:active={matchCase}
      title="Match case"
      aria-label="Match case"
      aria-pressed={matchCase}><CaseSensitive size={16} /></button
    >
    <button
      onclick={() => {
        wholeWord = !wholeWord;
        resetSearchPosition();
      }}
      class:active={wholeWord}
      title="Match whole word"
      aria-label="Match whole word"
      aria-pressed={wholeWord}><WholeWord size={16} /></button
    >
    {#if replacing}<LoaderCircle class="animate-spin" size={15} />{/if}
  </div>
  {#if error}<p role="alert">{error}</p>{/if}
</div>

<style>
  .search-replace {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.45rem;
    border: 1px solid var(--line);
    border-radius: 0.85rem;
    background: var(--paper);
    padding: 0.45rem;
  }
  .find-field,
  .replace-field {
    display: flex;
    min-width: 15rem;
    flex: 1;
    align-items: center;
    gap: 0.25rem;
    border: 1px solid var(--line);
    border-radius: 0.6rem;
    background: var(--paper-strong);
    padding: 0.25rem 0.35rem;
  }
  :global(.search-icon) {
    flex: none;
    color: var(--muted);
  }
  input {
    min-width: 4rem;
    flex: 1;
    background: transparent;
    padding: 0.3rem 0.25rem;
    font-size: 0.75rem;
    outline: none;
  }
  .count {
    min-width: 4.5rem;
    text-align: right;
    font-size: 0.62rem;
    color: var(--muted);
    white-space: nowrap;
  }
  button {
    display: flex;
    align-items: center;
    justify-content: center;
    border-radius: 0.45rem;
    padding: 0.35rem 0.5rem;
    font-size: 0.68rem;
    font-weight: 700;
    white-space: nowrap;
  }
  button:hover:not(:disabled),
  button.active {
    background: var(--accent-soft);
    color: var(--accent);
  }
  button:disabled {
    opacity: 0.35;
  }
  .replace-field button {
    border: 1px solid var(--line);
  }
  .options {
    display: flex;
    align-items: center;
    gap: 0.15rem;
    color: var(--muted);
  }
  .scope-switch {
    display: flex;
    align-items: center;
    gap: 0.15rem;
    border: 1px solid var(--line);
    border-radius: 0.55rem;
    padding: 0.15rem;
    margin-right: 0.25rem;
  }
  p {
    flex-basis: 100%;
    padding: 0.1rem 0.35rem;
    font-size: 0.68rem;
    color: #ef4444;
  }
</style>
