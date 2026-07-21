<script lang="ts">
  import { CaseSensitive, ChevronDown, ChevronUp, LoaderCircle, Search, WholeWord } from '@lucide/svelte';
  import { tick } from 'svelte';
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
    label = 'editable text'
  }: {
    texts: string[];
    onreplace: (updates: TextReplacement[]) => void | Promise<void>;
    onnavigate?: (match: TextSearchMatch) => void | Promise<void>;
    onactivate?: () => void | Promise<void>;
    disabled?: boolean;
    label?: string;
  } = $props();

  let query = $state('');
  let replacement = $state('');
  let matchCase = $state(false);
  let wholeWord = $state(false);
  let activeIndex = $state(0);
  let replacing = $state(false);
  let error = $state('');

  const matches = $derived(findTextMatches(texts, query, { matchCase, wholeWord }));
  const currentIndex = $derived(matches.length ? Math.min(activeIndex, matches.length - 1) : 0);
  const currentMatch = $derived(matches[currentIndex]);

  function resetSearchPosition() {
    activeIndex = 0;
    error = '';
  }

  async function activate() {
    await onactivate?.();
  }

  async function navigate(step: number) {
    if (!matches.length) return;
    activeIndex = (currentIndex + step + matches.length) % matches.length;
    await onnavigate?.(matches[activeIndex]);
  }

  function searchKeydown(event: KeyboardEvent) {
    if (event.key !== 'Enter') return;
    event.preventDefault();
    navigate(event.shiftKey ? -1 : 1);
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
      error = caught instanceof Error ? caught.message : String(caught);
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

<div class="search-replace" aria-label={`Search and replace ${label}`}>
  <div class="find-field">
    <Search class="search-icon" size={15}/>
    <input
      value={query}
      onfocus={activate}
      oninput={(event) => { query = event.currentTarget.value; resetSearchPosition(); }}
      onkeydown={searchKeydown}
      placeholder="Find"
      aria-label={`Find in ${label}`}
    />
    <span class="count" aria-live="polite">{query ? (matches.length ? `${currentIndex + 1} / ${matches.length}` : 'No matches') : ''}</span>
    <button onclick={() => navigate(-1)} disabled={!matches.length} title="Previous match" aria-label="Previous match"><ChevronUp size={15}/></button>
    <button onclick={() => navigate(1)} disabled={!matches.length} title="Next match" aria-label="Next match"><ChevronDown size={15}/></button>
  </div>
  <div class="replace-field">
    <input bind:value={replacement} onfocus={activate} placeholder="Replace with" aria-label={`Replace in ${label}`}/>
    <button onclick={replaceCurrent} disabled={disabled || replacing || !currentMatch}>Replace</button>
    <button onclick={replaceAll} disabled={disabled || replacing || !matches.length}>Replace all</button>
  </div>
  <div class="options">
    <button onclick={() => { matchCase = !matchCase; resetSearchPosition(); }} class:active={matchCase} title="Match case" aria-label="Match case" aria-pressed={matchCase}><CaseSensitive size={16}/></button>
    <button onclick={() => { wholeWord = !wholeWord; resetSearchPosition(); }} class:active={wholeWord} title="Match whole word" aria-label="Match whole word" aria-pressed={wholeWord}><WholeWord size={16}/></button>
    {#if replacing}<LoaderCircle class="animate-spin" size={15}/>{/if}
  </div>
  {#if error}<p role="alert">{error}</p>{/if}
</div>

<style>
  .search-replace{display:flex;flex-wrap:wrap;align-items:center;gap:.45rem;border:1px solid var(--line);border-radius:.85rem;background:var(--paper);padding:.45rem}.find-field,.replace-field{display:flex;min-width:15rem;flex:1;align-items:center;gap:.25rem;border:1px solid var(--line);border-radius:.6rem;background:var(--paper-strong);padding:.25rem .35rem}:global(.search-icon){flex:none;color:var(--muted)}input{min-width:4rem;flex:1;background:transparent;padding:.3rem .25rem;font-size:.75rem;outline:none}.count{min-width:4.5rem;text-align:right;font-size:.62rem;color:var(--muted);white-space:nowrap}button{display:flex;align-items:center;justify-content:center;border-radius:.45rem;padding:.35rem .5rem;font-size:.68rem;font-weight:700;white-space:nowrap}button:hover:not(:disabled),button.active{background:var(--accent-soft);color:var(--accent)}button:disabled{opacity:.35}.replace-field button{border:1px solid var(--line)}.options{display:flex;align-items:center;gap:.15rem;color:var(--muted)}p{flex-basis:100%;padding:.1rem .35rem;font-size:.68rem;color:#ef4444}
</style>
