<script lang="ts">
  import { errorMessage } from '$lib/errors';
  import {
    Check,
    CircleAlert,
    Languages,
    Pencil,
    Plus,
    Search,
    Sparkles,
    Trash2,
    X
  } from '@lucide/svelte';
  import { onMount } from 'svelte';
  import { pronunciationApi } from '$lib/admin-api';
  import type { SessionRecord } from '$lib/api-models';
  import { modalFocus } from '$lib/modal-focus';
  import { sessionApi } from '$lib/domain-api';

  type Pronunciation = {
    id: string;
    scope: 'global' | 'session';
    session_id: string | null;
    session_name: string | null;
    source_form: string;
    language: string;
    phonetic: string;
    backend: string;
    status: 'proposed' | 'reviewed' | 'disabled';
    source: string;
    notes: string | null;
    metadata: Record<string, unknown>;
    revision: number;
    updated_at: string;
  };

  type FormState = {
    source_form: string;
    phonetic: string;
    language: string;
    backend: string;
    scope: 'global' | 'session';
    session_id: string;
    status: 'proposed' | 'reviewed' | 'disabled';
    notes: string;
  };

  const blankForm = (): FormState => ({
    source_form: '',
    phonetic: '',
    language: 'und',
    backend: '*',
    scope: 'global',
    session_id: '',
    status: 'reviewed',
    notes: ''
  });

  let items = $state<Pronunciation[]>([]);
  let sessions = $state<SessionRecord[]>([]);
  let loading = $state(true);
  let saving = $state(false);
  let error = $state('');
  let query = $state('');
  let status = $state('');
  let scope = $state('');
  let language = $state('');
  let editorOpen = $state(false);
  let editing = $state<Pronunciation | null>(null);
  let form = $state<FormState>(blankForm());

  const proposedCount = $derived(
    items.filter((item) => item.status === 'proposed').length
  );
  const reviewedCount = $derived(
    items.filter((item) => item.status === 'reviewed').length
  );
  const globalCount = $derived(
    items.filter((item) => item.scope === 'global').length
  );

  function rendered(phonetic: string) {
    return phonetic.replaceAll('-', '');
  }

  function dateLabel(value: string) {
    return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium' }).format(
      new Date(value)
    );
  }

  async function load() {
    loading = true;
    error = '';
    try {
      const parameters = new URLSearchParams();
      if (query.trim()) parameters.set('q', query.trim());
      if (status) parameters.set('status', status);
      if (scope) parameters.set('scope', scope);
      if (language.trim()) parameters.set('language', language.trim());
      const result = await pronunciationApi.list<Pronunciation>(parameters);
      items = result.items;
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      loading = false;
    }
  }

  function createEntry() {
    editing = null;
    form = blankForm();
    editorOpen = true;
  }

  function editEntry(item: Pronunciation) {
    editing = item;
    form = {
      source_form: item.source_form,
      phonetic: item.phonetic,
      language: item.language,
      backend: item.backend,
      scope: item.scope,
      session_id: item.session_id ?? '',
      status: item.status,
      notes: item.notes ?? ''
    };
    editorOpen = true;
  }

  async function save() {
    if (!form.source_form.trim() || !form.phonetic.trim()) return;
    saving = true;
    error = '';
    const payload = {
      ...form,
      source_form: form.source_form.trim(),
      phonetic: form.phonetic.trim(),
      language: form.language.trim() || 'und',
      backend: form.backend.trim() || '*',
      session_id: form.scope === 'session' ? form.session_id || null : null,
      notes: form.notes.trim() || null
    };
    try {
      if (editing) {
        await pronunciationApi.update<Pronunciation>(
          editing.id,
          editing.revision,
          payload
        );
      } else {
        await pronunciationApi.create<Pronunciation>(payload);
      }
      editorOpen = false;
      await load();
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      saving = false;
    }
  }

  async function setStatus(item: Pronunciation, next: Pronunciation['status']) {
    error = '';
    try {
      await pronunciationApi.update<Pronunciation>(item.id, item.revision, {
        status: next
      });
      await load();
    } catch (caught) {
      error = errorMessage(caught);
    }
  }

  async function remove(item: Pronunciation) {
    if (!confirm(`Delete the pronunciation for “${item.source_form}”?`)) return;
    error = '';
    try {
      await pronunciationApi.remove(item.id, item.revision);
      await load();
    } catch (caught) {
      error = errorMessage(caught);
    }
  }

  onMount(async () => {
    const sessionResult = await sessionApi
      .list()
      .catch(() => ({ items: [] as SessionRecord[] }));
    sessions = sessionResult.items;
    await load();
  });
</script>

<svelte:head><title>Pronunciations — Pandrator</title></svelte:head>

<div class="mx-auto max-w-[92rem]">
  <header class="flex flex-wrap items-end justify-between gap-5">
    <div class="max-w-3xl">
      <div class="eyebrow">Speech workspace</div>
      <h1 class="mt-2 text-3xl font-semibold tracking-tight sm:text-4xl">
        Pronunciation library
      </h1>
      <p class="muted mt-3 max-w-2xl leading-relaxed">
        Review phonetic spellings before Pandrator reuses them. Hyphens make
        syllables readable here; the speech renderer removes them
        deterministically.
      </p>
    </div>
    <button onclick={createEntry} class="btn btn-primary"
      ><Plus size={17} /> Add pronunciation</button
    >
  </header>

  <section class="mt-7 grid gap-3 sm:grid-cols-3">
    <div class="metric-card">
      <span>Needs review</span><strong>{proposedCount}</strong><small
        >Model suggestions remain inactive</small
      >
    </div>
    <div class="metric-card">
      <span>Ready to reuse</span><strong>{reviewedCount}</strong><small
        >Reviewed entries only</small
      >
    </div>
    <div class="metric-card">
      <span>Available everywhere</span><strong>{globalCount}</strong><small
        >Global scope</small
      >
    </div>
  </section>

  <section class="surface mt-5 overflow-hidden rounded-3xl">
    <form
      class="grid gap-3 border-b border-[var(--line)] p-4 lg:grid-cols-[minmax(12rem,1fr)_8.5rem_8.5rem_7.5rem_6.5rem]"
      onsubmit={(event) => {
        event.preventDefault();
        load();
      }}
    >
      <label class="search-field"
        ><Search size={16} /><input
          bind:value={query}
          aria-label="Search pronunciations"
          placeholder="Search written or phonetic form"
        /></label
      >
      <select bind:value={status} aria-label="Status filter"
        ><option value="">All statuses</option><option value="proposed"
          >Needs review</option
        ><option value="reviewed">Reviewed</option><option value="disabled"
          >Disabled</option
        ></select
      >
      <select bind:value={scope} aria-label="Scope filter"
        ><option value="">All scopes</option><option value="global"
          >Global</option
        ><option value="session">Session</option></select
      >
      <input
        bind:value={language}
        placeholder="Language, e.g. en"
        aria-label="Language filter"
      />
      <button class="btn btn-secondary"><Search size={15} /> Filter</button>
    </form>

    {#if error}
      <div
        class="flex items-start gap-2 border-b border-red-400/30 bg-red-500/10 px-5 py-3 text-sm text-red-600"
      >
        <CircleAlert class="mt-0.5 shrink-0" size={16} />{error}
      </div>
    {/if}

    {#if loading}
      <div class="grid min-h-64 place-items-center">
        <span class="eyebrow animate-pulse">Loading pronunciations…</span>
      </div>
    {:else if !items.length}
      <div class="grid min-h-72 place-items-center px-6 text-center">
        <div>
          <Languages class="mx-auto text-[var(--accent)]" size={34} />
          <h2 class="mt-4 text-xl font-semibold">No matching pronunciations</h2>
          <p class="muted mt-2 text-sm">
            Add one manually, or let speech planning propose entries for review.
          </p>
        </div>
      </div>
    {:else}
      <div class="divide-y divide-[var(--line)]">
        {#each items as item (item.id)}
          <article
            class:inactive={item.status === 'disabled'}
            class="entry-row grid gap-4 px-5 py-4 lg:grid-cols-[minmax(12rem,1fr)_minmax(14rem,1.2fr)_minmax(12rem,.8fr)_auto] lg:items-center"
          >
            <div class="min-w-0">
              <div class="flex flex-wrap items-center gap-2">
                <strong class="truncate text-base">{item.source_form}</strong
                ><span class={`state ${item.status}`}
                  >{item.status === 'proposed'
                    ? 'needs review'
                    : item.status}</span
                >
              </div>
              <div class="muted mt-1 flex flex-wrap gap-x-2 text-xs">
                <span>{item.language}</span><span>·</span><span
                  >{item.backend === '*'
                    ? 'Any TTS backend'
                    : item.backend}</span
                ><span>·</span><span>{dateLabel(item.updated_at)}</span>
              </div>
            </div>
            <div>
              <div
                class="font-mono text-sm font-semibold tracking-wide text-[var(--accent)]"
              >
                {item.phonetic}
              </div>
              <div class="muted mt-1 text-xs">
                Sent as <span class="font-mono text-[var(--ink)]"
                  >{rendered(item.phonetic)}</span
                >
              </div>
            </div>
            <div class="min-w-0 text-xs">
              <div class="font-semibold">
                {item.scope === 'global'
                  ? 'Global library'
                  : item.session_name || 'Session override'}
              </div>
              {#if item.notes}<p class="muted mt-1 line-clamp-2">
                  {item.notes}
                </p>{:else if item.source === 'speech_plan'}<p
                  class="muted mt-1"
                >
                  Suggested by a validated speech plan.
                </p>{/if}
            </div>
            <div class="flex flex-wrap justify-end gap-2">
              {#if item.status === 'proposed'}<button
                  onclick={() => setStatus(item, 'reviewed')}
                  class="btn btn-sm btn-primary"
                  ><Check size={14} /> Approve</button
                >{/if}
              {#if item.status === 'reviewed'}<button
                  onclick={() => setStatus(item, 'disabled')}
                  class="btn btn-sm btn-secondary">Disable</button
                >{:else if item.status === 'disabled'}<button
                  onclick={() => setStatus(item, 'reviewed')}
                  class="btn btn-sm btn-secondary">Enable</button
                >{/if}
              <button
                onclick={() => editEntry(item)}
                class="btn btn-sm btn-secondary"
                aria-label={`Edit ${item.source_form}`}
                ><Pencil size={14} /></button
              >
              <button
                onclick={() => remove(item)}
                class="btn btn-sm btn-secondary danger"
                aria-label={`Delete ${item.source_form}`}
                ><Trash2 size={14} /></button
              >
            </div>
          </article>
        {/each}
      </div>
    {/if}
  </section>
</div>

{#if editorOpen}
  <div
    class="fixed inset-0 z-[100] grid place-items-center bg-black/55 p-3 backdrop-blur-sm"
    role="presentation"
    onclick={(event) =>
      event.target === event.currentTarget && (editorOpen = false)}
  >
    <div
      use:modalFocus={{ onclose: () => (editorOpen = false) }}
      class="surface modal-panel flex w-full max-w-2xl flex-col"
      role="dialog"
      aria-modal="true"
      aria-labelledby="pronunciation-editor-title"
    >
      <form
        class="flex min-h-0 flex-1 flex-col"
        onsubmit={(event) => {
          event.preventDefault();
          save();
        }}
      >
        <header
          class="flex items-start gap-4 border-b border-[var(--line)] px-6 py-5"
        >
          <div class="min-w-0 flex-1">
            <div class="eyebrow">{editing ? 'Edit entry' : 'New entry'}</div>
            <h2
              id="pronunciation-editor-title"
              class="mt-1 text-2xl font-semibold"
            >
              {editing ? editing.source_form : 'Add a pronunciation'}
            </h2>
          </div>
          <button
            type="button"
            onclick={() => (editorOpen = false)}
            class="btn btn-icon btn-quiet"
            aria-label="Close"><X size={20} /></button
          >
        </header>
        <div class="modal-scroll grid gap-5 p-6 sm:grid-cols-2">
          <label class="field sm:col-span-2"
            ><span>Written form</span><input
              bind:value={form.source_form}
              required
              placeholder="Imaoka"
            /></label
          >
          <label class="field sm:col-span-2"
            ><span>Structured pronunciation</span><input
              bind:value={form.phonetic}
              required
              pattern="[a-z]+(?:-[a-z]+)*(?: [a-z]+(?:-[a-z]+)*)*"
              placeholder="ee-mah-oh-kah"
            /><small>Lowercase ASCII syllables separated by hyphens.</small
            ></label
          >
          <div class="preview sm:col-span-2">
            <Sparkles size={16} /><span
              ><strong>TTS rendering:</strong>
              {form.phonetic ? rendered(form.phonetic) : '—'}</span
            >
          </div>
          <label class="field"
            ><span>Language</span><input
              bind:value={form.language}
              placeholder="und"
            /></label
          >
          <label class="field"
            ><span>TTS backend</span><input
              bind:value={form.backend}
              placeholder="*"
            /><small>Use * for every backend.</small></label
          >
          <label class="field"
            ><span>Scope</span><select bind:value={form.scope}
              ><option value="global">Global</option><option value="session"
                >One session</option
              ></select
            ></label
          >
          {#if form.scope === 'session'}<label class="field"
              ><span>Session</span><select bind:value={form.session_id} required
                ><option value="">Choose a session</option
                >{#each sessions as session}<option value={session.id}
                    >{session.name}</option
                  >{/each}</select
              ></label
            >{/if}
          <label class="field"
            ><span>Status</span><select bind:value={form.status}
              ><option value="reviewed">Reviewed</option><option
                value="proposed">Needs review</option
              ><option value="disabled">Disabled</option></select
            ></label
          >
          <label class="field sm:col-span-2"
            ><span>Notes</span><textarea
              bind:value={form.notes}
              rows="3"
              placeholder="Language, source, or voice-specific guidance"
            ></textarea></label
          >
        </div>
        <footer
          class="flex justify-end gap-3 border-t border-[var(--line)] px-6 py-4"
        >
          <button
            type="button"
            onclick={() => (editorOpen = false)}
            class="btn btn-secondary">Cancel</button
          ><button
            disabled={saving ||
              !form.source_form.trim() ||
              !form.phonetic.trim()}
            class="btn btn-primary"
            ><Check size={16} />{saving
              ? 'Saving…'
              : 'Save pronunciation'}</button
          >
        </footer>
      </form>
    </div>
  </div>
{/if}

<style>
  .metric-card {
    display: grid;
    grid-template-columns: 1fr auto;
    gap: 0.15rem 0.8rem;
    border: 1px solid var(--line);
    border-radius: 1.15rem;
    background: color-mix(in srgb, var(--paper-strong) 92%, transparent);
    padding: 1rem 1.1rem;
  }
  .metric-card span {
    font-size: 0.72rem;
    font-weight: 750;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--muted);
  }
  .metric-card strong {
    grid-row: span 2;
    font-size: 1.8rem;
    line-height: 1;
    color: var(--accent);
  }
  .metric-card small {
    font-size: 0.72rem;
    color: var(--muted);
  }
  input,
  select,
  textarea {
    width: 100%;
    border: 1px solid var(--line);
    border-radius: 0.75rem;
    background: var(--paper);
    padding: 0.68rem 0.8rem;
    font-size: 0.82rem;
  }
  .search-field {
    display: flex;
    align-items: center;
    gap: 0.55rem;
    border: 1px solid var(--line);
    border-radius: 0.75rem;
    background: var(--paper);
    padding: 0 0.75rem;
  }
  .search-field input {
    border: 0;
    background: transparent;
    padding-left: 0;
    outline: 0;
  }
  .entry-row.inactive {
    opacity: 0.56;
  }
  .state {
    border-radius: 999px;
    padding: 0.2rem 0.48rem;
    font-size: 0.58rem;
    font-weight: 800;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }
  .state.proposed {
    background: rgba(245, 158, 11, 0.15);
    color: #a16207;
  }
  .state.reviewed {
    background: color-mix(in srgb, var(--success) 15%, transparent);
    color: var(--success);
  }
  .state.disabled {
    background: var(--accent-soft);
    color: var(--muted);
  }
  .danger:hover {
    border-color: rgba(239, 68, 68, 0.35);
    color: #dc2626;
  }
  .field {
    display: flex;
    flex-direction: column;
    gap: 0.45rem;
  }
  .field > span {
    font-size: 0.75rem;
    font-weight: 750;
  }
  .field small {
    color: var(--muted);
    font-size: 0.68rem;
  }
  .preview {
    display: flex;
    align-items: center;
    gap: 0.55rem;
    border: 1px solid color-mix(in srgb, var(--accent) 30%, var(--line));
    border-radius: 0.9rem;
    background: var(--accent-soft);
    padding: 0.8rem 1rem;
    color: var(--accent);
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.78rem;
  }
</style>
