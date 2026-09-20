<script lang="ts">
  import { onDestroy, onMount, tick } from 'svelte';
  import { beforeNavigate, goto } from '$app/navigation';
  import { page as route } from '$app/state';
  import {
    ArrowLeft,
    Filter,
    FolderPlus,
    Library,
    LoaderCircle,
    Play,
    Plus,
    Search,
    SlidersHorizontal,
    WandSparkles,
    X
  } from '@lucide/svelte';
  import { speechServiceApi, voiceApi } from './admin-api';
  import { jobApi } from './domain-api';
  import { errorMessage } from './errors';
  import type { TtsService, VoiceRecord } from './api-models';
  import {
    emptyVoiceProfile,
    voiceFacetLabel,
    voiceLibraryApi,
    type CatalogVoice,
    type VoiceCatalogPage,
    type VoiceCollection,
    type VoiceProfile
  } from './voice-library-api';
  import VoiceManager from './VoiceManager.svelte';
  import VoiceProfileEditor from './VoiceProfileEditor.svelte';
  import VoiceDesignDialog from './VoiceDesignDialog.svelte';
  import AudioPlayer from './AudioPlayer.svelte';
  import { modalFocus } from './modal-focus';

  let {
    initialService = '',
    initialModel = '',
    initialVoice = '',
    initialView,
    embedded = false,
    onselect,
    onvoicepublished
  }: {
    initialService?: string;
    initialModel?: string;
    initialVoice?: string;
    initialView?: 'references' | 'prebuilt';
    embedded?: boolean;
    onselect?: (voice: CatalogVoice) => void;
    onvoicepublished?: (providerVoiceId: string) => void;
  } = $props();
  let query = $state('');
  let language = $state('');
  let accent = $state('');
  let category = $state('');
  let pitch = $state('');
  let useCase = $state('');
  let texture = $state('');
  let collectionId = $state('');
  let kind = $state('all');
  let service = $state('');
  let model = $state('');
  let readyOnly = $state(false);
  let reviewedOnly = $state(false);
  let sort = $state('relevance');
  let filtersOpen = $state(false);
  let page = $state<VoiceCatalogPage | null>(null);
  let collections = $state<VoiceCollection[]>([]);
  let services = $state<TtsService[]>([]);
  let voices = $state<VoiceRecord[]>([]);
  let loading = $state(false);
  let error = $state('');
  let notice = $state('');
  let detail = $state<CatalogVoice | null>(null);
  let editing = $state(false);
  let samplesOpen = $state(false);
  let editName = $state('');
  let editDescription = $state('');
  let editCategory = $state('unspecified');
  let profile = $state<VoiceProfile>(emptyVoiceProfile());
  let editBaseline = $state('');
  let saving = $state(false);
  let collectionName = $state('');
  let collectionForm = $state(false);
  let collectionBusy = $state(false);
  let designerOpen = $state(false);
  let compare = $state<CatalogVoice[]>([]);
  let auditionText = $state(
    'The fire was burning low, but there was still time to tell the story.'
  );
  let auditionBusy = $state(false);
  let activeJob = $state('');
  let stopAuditions = false;
  let comparisonAudio = $state<
    Record<string, { artifactId: string; context: string }>
  >({});
  const comparisonContext = $derived(
    JSON.stringify({ service, model, language, auditionText })
  );
  let filterPanel = $state<HTMLElement>();
  $effect(() => {
    if (!filtersOpen || !filterPanel) return;
    const focus = modalFocus(filterPanel, {
      onclose: () => (filtersOpen = false)
    });
    return () => focus?.destroy?.();
  });
  let playing = $state('');
  let pendingClose = $state<(() => void) | null>(null);
  let initialized = $state(false);
  let sequence = 0;
  let alive = true;
  let opener: HTMLButtonElement | null = null;
  let scrollY = 0;
  let libraryRoot: HTMLDivElement;
  let scrollContainer: HTMLElement | null = null;
  const dirty = $derived(
    editing &&
      JSON.stringify({ editName, editDescription, editCategory, profile }) !==
        editBaseline
  );
  const parameters = $derived({
    query,
    language,
    accent,
    voice_category: category,
    pitch,
    texture,
    use_case: useCase,
    collection_id: collectionId,
    kind,
    service_id: service,
    model,
    ready_only: readyOnly,
    reviewed_only: reviewedOnly,
    sort
  });
  const selectedService = $derived(services.find((s) => s.id === service));
  const filterCount = $derived(
    [
      language,
      accent,
      category,
      pitch,
      texture,
      useCase,
      service,
      readyOnly,
      reviewedOnly
    ].filter(Boolean).length
  );
  const languages = $derived(Object.keys(page?.facets.language ?? {}).sort());

  async function load(cursor?: string) {
    const ticket = ++sequence;
    const request = { ...parameters, limit: 30, cursor };
    loading = true;
    error = '';
    try {
      const result = await voiceLibraryApi.query(request);
      if (!alive || ticket !== sequence) return;
      page =
        cursor && page
          ? { ...result, items: [...page.items, ...result.items] }
          : result;
    } catch (caught) {
      if (alive && ticket === sequence) error = errorMessage(caught);
    } finally {
      if (alive && ticket === sequence) loading = false;
    }
  }
  $effect(() => {
    void parameters;
    if (!initialized) return;
    const timeout = setTimeout(() => void load(), 220);
    return () => clearTimeout(timeout);
  });
  async function refreshDetail() {
    await refreshAuxiliary();
    await load();
    if (detail?.kind === 'managed' && !editing) {
      const id = detail.id;
      const result = await voiceLibraryApi.query({
        query: id,
        kind: 'managed',
        limit: 1
      });
      if (detail?.id === id)
        detail = result.items.find((v) => v.id === id) ?? null;
    }
  }
  async function closeSamples() {
    samplesOpen = false;
    try {
      await refreshDetail();
    } catch (caught) {
      error = errorMessage(caught);
    }
  }
  async function refreshAuxiliary() {
    const [groupPayload, servicePayload, library] = await Promise.all([
      voiceLibraryApi.collections(),
      speechServiceApi.catalogue(false),
      voiceApi.list<VoiceRecord>()
    ]);
    if (!alive) return;
    collections = groupPayload.items;
    services = servicePayload.services;
    voices = library.items;
  }
  onMount(() => {
    service = initialService || route.url.searchParams.get('service') || '';
    model = initialModel || route.url.searchParams.get('model') || '';
    kind = initialView === 'prebuilt' ? 'provider' : 'all';
    initialized = true;
    void refreshAuxiliary().catch((caught) => (error = errorMessage(caught)));
    if (initialVoice)
      void voiceLibraryApi
        .query({ query: initialVoice, kind: 'managed', limit: 1 })
        .then((result) => {
          const match = result.items.find((item) => item.id === initialVoice);
          if (alive && match) openDetail(match);
        })
        .catch((caught) => (error = errorMessage(caught)));
  });
  onDestroy(() => {
    alive = false;
    stopAuditions = true;
  });
  export function requestClose(close: () => void) {
    if (dirty) pendingClose = close;
    else close();
  }
  beforeNavigate((event) => {
    if (!dirty) return;
    if (event.willUnload) {
      event.cancel();
      return;
    }
    const url = event.to?.url;
    if (url) {
      event.cancel();
      pendingClose = () => {
        editing = false;
        void goto(url.href);
      };
    }
  });
  function openDetail(voice: CatalogVoice, target?: HTMLButtonElement) {
    opener = target ?? null;
    scrollContainer =
      libraryRoot?.closest<HTMLElement>('.modal-scroll') ?? null;
    scrollY = scrollContainer?.scrollTop ?? window.scrollY;
    detail = voice;
    editing = false;
    samplesOpen = false;
    notice = '';
    error = '';
    void tick().then(() => {
      if (scrollContainer) scrollContainer.scrollTo({ top: 0 });
      else window.scrollTo({ top: 0 });
    });
  }
  function closeDetail() {
    requestClose(async () => {
      detail = null;
      editing = false;
      samplesOpen = false;
      await tick();
      if (scrollContainer) scrollContainer.scrollTo({ top: scrollY });
      else window.scrollTo({ top: scrollY });
      opener?.focus({ preventScroll: true });
    });
  }
  function edit() {
    if (!detail) return;
    editName = detail.name;
    editDescription = detail.description;
    editCategory = detail.voice_category;
    profile = structuredClone($state.snapshot(detail.profile));
    editBaseline = JSON.stringify({
      editName,
      editDescription,
      editCategory,
      profile
    });
    editing = true;
  }
  async function save() {
    if (!detail) return false;
    saving = true;
    error = '';
    try {
      const result = await voiceLibraryApi.update(detail, {
        name: editName.trim(),
        description: editDescription.trim() || null,
        voice_category: editCategory,
        profile
      });
      detail = {
        ...detail,
        name: editName.trim(),
        description: editDescription.trim(),
        voice_category: editCategory,
        profile: structuredClone($state.snapshot(profile)),
        revision: result.revision
      };
      editing = false;
      notice = 'Voice profile saved.';
      await load();
      await refreshAuxiliary();
      return true;
    } catch (caught) {
      error = errorMessage(caught);
      return false;
    } finally {
      saving = false;
    }
  }
  async function createCollection() {
    if (!collectionName.trim()) return;
    collectionBusy = true;
    error = '';
    try {
      const created = await voiceLibraryApi.createCollection(
        collectionName.trim()
      );
      collections = [...collections, created];
      collectionName = '';
      collectionForm = false;
      collectionId = created.id;
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      collectionBusy = false;
    }
  }
  async function membership(collection: VoiceCollection, add: boolean) {
    if (!detail) return;
    collectionBusy = true;
    error = '';
    try {
      const updated = await voiceLibraryApi.membership(
        collection,
        detail.reference,
        add
      );
      collections = collections.map((c) => (c.id === updated.id ? updated : c));
      detail = {
        ...detail,
        collections: add
          ? [
              ...detail.collections.filter((c) => c.id !== collection.id),
              { id: collection.id, name: collection.name }
            ]
          : detail.collections.filter((c) => c.id !== collection.id)
      };
      await load();
    } catch (caught) {
      error = errorMessage(caught);
      await refreshAuxiliary();
    } finally {
      collectionBusy = false;
    }
  }
  function selectVoice(voice: CatalogVoice) {
    if (onselect) {
      onselect(voice);
      return;
    }
    const registration = voice.compatibility.find(
      (c) =>
        (!service || c.service_id === service) &&
        (!model || c.model === model) &&
        c.ready
    );
    if (registration?.voice) onvoicepublished?.(registration.voice);
    else
      error =
        'Choose a compatible renderer and prepare this voice before assigning it.';
  }
  function toggleCompare(voice: CatalogVoice) {
    if (compare.some((v) => v.key === voice.key))
      compare = compare.filter((v) => v.key !== voice.key);
    else if (compare.length < 3) compare = [...compare, voice];
  }
  async function auditions() {
    if (!service || !model || !auditionText.trim()) return;
    auditionBusy = true;
    stopAuditions = false;
    error = '';
    notice = '';
    const renderer = service,
      rendererModel = model,
      text = auditionText.trim(),
      auditionLanguage = language || 'en',
      context = comparisonContext;
    const shortlist = [...compare];
    try {
      for (const voice of shortlist) {
        if (!alive || stopAuditions) break;
        const binding = voice.compatibility.find(
          (c) => c.service_id === renderer && c.model === rendererModel
        );
        if (!binding?.ready || !binding.voice)
          throw new Error(
            `${voice.name} is not ready for this renderer. Open its samples and provider links first.`
          );
        const queued = await speechServiceApi.preview(renderer, {
          text,
          model: rendererModel,
          voice: binding.voice,
          language: auditionLanguage
        });
        activeJob = queued.id;
        let finished = false;
        for (
          let attempt = 0;
          attempt < 3600 && alive && !stopAuditions;
          attempt++
        ) {
          const job = await jobApi.get(queued.id);
          if (job.status === 'succeeded') {
            const artifact = String(job.result_json?.artifact_id ?? '');
            if (!artifact)
              throw new Error('The audition completed without audio.');
            comparisonAudio = {
              ...comparisonAudio,
              [voice.key]: { artifactId: artifact, context }
            };
            finished = true;
            break;
          }
          if (['failed', 'canceled', 'interrupted'].includes(job.status))
            throw new Error(job.error_message || `Audition ${job.status}.`);
          await new Promise((resolve) => setTimeout(resolve, 1000));
        }
        if (!finished && !stopAuditions)
          throw new Error(
            'The audition is still running. Follow it in Activity & logs.'
          );
        activeJob = '';
      }
    } catch (caught) {
      if (alive) error = errorMessage(caught);
    } finally {
      if (alive) auditionBusy = false;
    }
  }
  async function cancelAuditions() {
    stopAuditions = true;
    if (activeJob)
      try {
        await jobApi.cancel(activeJob);
      } catch (caught) {
        error = errorMessage(caught);
      }
    activeJob = '';
    auditionBusy = false;
  }
  function clearFilters() {
    language = '';
    accent = '';
    category = '';
    pitch = '';
    texture = '';
    useCase = '';
    readyOnly = false;
    reviewedOnly = false;
    service = initialService;
    model = initialModel;
  }
  function rendererLabel(voice: CatalogVoice) {
    if (voice.reference.kind !== 'provider')
      return `${voice.sample_count ?? 0} reference samples`;
    const ref = voice.reference;
    return `${services.find((s) => s.id === ref.service_id)?.name || ref.service_id} · ${ref.model}`;
  }
  function traits(voice: CatalogVoice) {
    return [
      voice.voice_category !== 'unspecified'
        ? voiceFacetLabel(voice.voice_category)
        : '',
      voice.profile.pitch
        ? `${voiceFacetLabel(voice.profile.pitch)} pitch`
        : '',
      ...voice.profile.textures.slice(0, 2)
    ].filter(Boolean);
  }
</script>

<div
  bind:this={libraryRoot}
  class="catalog-library mx-auto w-full max-w-7xl"
  class:embedded
>
  {#if !detail && !samplesOpen}
    <header class="mb-6 flex flex-wrap items-end justify-between gap-4">
      <div>
        <div class="eyebrow">Your cast starts here</div>
        <h1 class="mt-2 text-3xl font-semibold sm:text-4xl">Voice library</h1>
        <p class="muted mt-2 max-w-2xl text-sm">
          Find a familiar voice, audition a new one, or design the speaker you
          have in mind.
        </p>
      </div>
      <div class="flex flex-wrap gap-2">
        <button class="btn btn-primary" onclick={() => (designerOpen = true)}
          ><WandSparkles size={16} />Design voice</button
        ><button
          class="btn btn-secondary"
          onclick={() => {
            samplesOpen = true;
            detail = null;
          }}><Plus size={16} />Add reference</button
        >
      </div>
    </header>
  {:else}
    <button
      class="muted mb-5 flex min-h-10 items-center gap-2 text-sm font-semibold"
      onclick={() => (detail ? closeDetail() : void closeSamples())}
      ><ArrowLeft size={16} />Back to voices</button
    >
  {/if}
  {#if error}<p
      role="alert"
      class="mb-4 rounded-xl border border-red-400/40 bg-red-500/10 p-4 text-sm"
    >
      {error}
    </p>{/if}
  {#if notice}<p
      role="status"
      class="mb-4 rounded-xl bg-[var(--accent-soft)] p-4 text-sm"
    >
      {notice}
    </p>{/if}

  {#if detail}
    <article class="surface rounded-2xl p-4 sm:p-7">
      <div class="flex flex-wrap items-start justify-between gap-4">
        <div class="min-w-0">
          <div class="eyebrow">
            {detail.kind === 'managed' ? 'Saved voice' : 'Provider voice'}
          </div>
          <h2 class="mt-2 break-words text-2xl font-semibold">{detail.name}</h2>
          <p class="muted mt-2 text-sm">
            {detail.description || 'Add a description to help with casting.'}
          </p>
        </div>
        <div class="flex flex-wrap gap-2">
          {#if !detail.bundled && !editing}<button
              class="btn btn-secondary"
              onclick={edit}><SlidersHorizontal size={15} />Edit profile</button
            >{/if}{#if onselect || onvoicepublished}<button
              class="btn btn-primary"
              onclick={() => selectVoice(detail!)}>Use this voice</button
            >{/if}
        </div>
      </div>
      {#if detail.preview_artifact_id}<div class="mt-5">
          <AudioPlayer
            src={`/api/v1/artifacts/${detail.preview_artifact_id}/content`}
            label={`${detail.name} reference`}
          />
        </div>{/if}
      {#if editing}
        <form
          class="mt-6 space-y-5"
          onsubmit={(event) => {
            event.preventDefault();
            void save();
          }}
        >
          <div class="grid gap-3 sm:grid-cols-2">
            <label class="text-sm font-semibold"
              >Name<input
                class="input mt-1 w-full"
                required
                maxlength="255"
                bind:value={editName}
              /></label
            ><label class="text-sm font-semibold"
              >Voice presentation<select
                class="input mt-1 w-full"
                bind:value={editCategory}
                onchange={() =>
                  (profile.evidence = {
                    ...profile.evidence,
                    voice_category: { source: 'user', status: 'described' }
                  })}
                >{#each ['unspecified', 'male', 'female', 'androgynous'] as value}<option
                    {value}>{voiceFacetLabel(value)}</option
                  >{/each}</select
              ></label
            >
          </div>
          <label class="block text-sm font-semibold"
            >Description<textarea
              class="input mt-1 w-full"
              rows="2"
              maxlength="4000"
              bind:value={editDescription}></textarea></label
          >
          <VoiceProfileEditor
            bind:profile
            disabled={saving}
            evidenceArtifactId={detail.preview_artifact_id || ''}
          />
          <div class="flex flex-wrap justify-end gap-2">
            <button
              type="button"
              class="btn btn-secondary"
              disabled={saving}
              onclick={() => requestClose(() => (editing = false))}
              >Cancel</button
            ><button
              class="btn btn-primary"
              disabled={saving || !editName.trim()}
              >{saving ? 'Saving…' : 'Save profile'}</button
            >
          </div>
        </form>
      {:else}
        <div class="mt-5 flex flex-wrap gap-2">
          {#each traits(detail) as trait}<span class="catalog-chip"
              >{trait}</span
            >{/each}{#each detail.profile.use_cases as use}<span
              class="catalog-chip">{voiceFacetLabel(use)}</span
            >{/each}
        </div>
        <div class="mt-4 space-y-2">
          {#each detail.profile.languages as lang}<p class="text-sm">
              <strong>{lang.language.toUpperCase()}</strong>{lang.accent
                ? ` · ${lang.accent}`
                : ''}<span class="muted ml-2 text-xs"
                >{lang.evidence?.status === 'reviewed'
                  ? 'Audition reviewed'
                  : lang.evidence?.status === 'requested'
                    ? 'Requested · needs audition'
                    : 'Described'}</span
              >
            </p>{/each}
        </div>
        <section class="mt-6 border-t border-[var(--line)] pt-5">
          <h3 class="font-semibold">Collections</h3>
          <div class="mt-3 flex flex-wrap gap-3">
            {#each collections as collection}<label
                class="flex min-h-10 items-center gap-2 text-sm"
                ><input
                  type="checkbox"
                  checked={detail.collections.some(
                    (c) => c.id === collection.id
                  )}
                  disabled={collectionBusy}
                  onchange={(event) =>
                    void membership(collection, event.currentTarget.checked)}
                />{collection.name}</label
              >{:else}<p class="muted text-sm">
                Create a collection from the library to group voices by project
                or use.
              </p>{/each}
          </div>
        </section>
        <details
          class="mt-6 border-t border-[var(--line)] pt-5"
          open={Boolean(service)}
        >
          <summary class="cursor-pointer font-semibold"
            >Renderers <span class="muted text-xs font-normal"
              >({detail.compatibility.length} models)</span
            ></summary
          >
          <div class="mt-3 space-y-2">
            {#each detail.compatibility as binding}<p
                class="break-words text-sm"
              >
                <span>{binding.service_id} · {binding.model}</span><span
                  class="muted ml-2 text-xs"
                  >{voiceFacetLabel(binding.status)}{binding.modes
                    .reference_with_instructions
                    ? ' · clone + direction'
                    : ''}</span
                >
              </p>{:else}<p class="muted text-sm">
                No compatible renderer is configured yet.
              </p>{/each}
          </div>
        </details>
        {#if detail.kind === 'managed'}<button
            class="btn mt-6"
            onclick={() =>
              samplesOpen ? void closeSamples() : (samplesOpen = true)}
            >{samplesOpen
              ? 'Hide sample tools'
              : 'Manage samples and provider links'}</button
          >{/if}
      {/if}
    </article>
    {#if samplesOpen && detail.kind === 'managed'}<div class="mt-5">
        <VoiceManager
          onback={() => void closeSamples()}
          initialVoice={detail.id}
          initialService={service}
          embedded
          detailOnly
          referencesOnly
          onvoicepublished={async (id) => {
            onvoicepublished?.(id);
            await refreshDetail();
          }}
        />
      </div>{/if}
  {:else if samplesOpen}
    <h2 class="mb-2 text-2xl font-semibold">Add a reference voice</h2>
    <p class="muted mb-5 text-sm">
      Create a voice or select one below to add a recording.
    </p>
    <VoiceManager
      onback={() => void closeSamples()}
      embedded
      referencesOnly
      initialService={service}
    />
  {:else}
    <div class="catalog-toolbar mb-4">
      <label
        class="search-field flex min-w-0 items-center gap-2 rounded-xl border border-[var(--line)] bg-[var(--paper-strong)] px-3"
        ><Search size={17} class="muted" /><input
          class="min-h-12 min-w-0 flex-1 bg-transparent text-sm outline-none"
          type="search"
          aria-label="Search voices"
          placeholder="Search names, accents, textures, tags…"
          bind:value={query}
        /></label
      >
      <button
        class="btn btn-secondary catalog-mobile-only"
        aria-expanded={filtersOpen}
        onclick={() => (filtersOpen = true)}
        ><Filter size={16} />Filters{filterCount
          ? ` (${filterCount})`
          : ''}</button
      >
      <label class="collection-field min-w-0"
        ><span class="sr-only">Collection</span><select
          class="input h-full min-h-12 w-full"
          bind:value={collectionId}
          ><option value="">All collections</option
          >{#each collections as collection}<option value={collection.id}
              >{collection.name} ({collection.member_count})</option
            >{/each}</select
        ></label
      >
      <button
        class="btn btn-icon"
        aria-label="Create collection"
        title="Create collection"
        onclick={() => (collectionForm = !collectionForm)}
        ><FolderPlus size={18} /></button
      >
    </div>
    {#if collectionForm}<form
        class="mb-4 flex flex-wrap gap-2 rounded-xl border border-[var(--line)] p-3"
        onsubmit={(event) => {
          event.preventDefault();
          void createCollection();
        }}
      >
        <label class="min-w-0 flex-1"
          ><span class="sr-only">Collection name</span><input
            class="input w-full"
            placeholder="e.g. A Christmas Carol"
            maxlength="255"
            bind:value={collectionName}
          /></label
        ><button
          class="btn btn-primary"
          disabled={collectionBusy || !collectionName.trim()}
          >Create collection</button
        ><button
          class="btn btn-secondary"
          type="button"
          onclick={() => (collectionForm = false)}>Cancel</button
        >
      </form>{/if}
    <div class="catalog-layout">
      <div
        bind:this={filterPanel}
        class="catalog-filters"
        class:filters-open={filtersOpen}
        role={filtersOpen ? 'dialog' : undefined}
        aria-modal={filtersOpen ? 'true' : undefined}
        aria-label="Voice filters"
      >
        <div class="mb-4 flex items-center justify-between">
          <h2 class="font-semibold">Find a voice</h2>
          <button
            class="btn btn-icon catalog-mobile-only"
            aria-label="Close filters"
            onclick={() => (filtersOpen = false)}><X size={18} /></button
          >
        </div>
        <div class="space-y-4">
          <label class="filter-label"
            >Origin<select class="input mt-1 w-full" bind:value={kind}
              ><option value="all">All voices</option><option value="managed"
                >Saved references</option
              ><option value="provider">Provider voices</option></select
            ></label
          >
          <label class="filter-label"
            >Language<select class="input mt-1 w-full" bind:value={language}
              ><option value="">Any language</option
              >{#each languages as lang}<option value={lang}
                  >{lang.toUpperCase()}</option
                >{/each}{#if language && !languages.includes(language)}<option
                  value={language}>{language.toUpperCase()}</option
                >{/if}</select
            ></label
          >
          <label class="filter-label"
            >Accent<input
              class="input mt-1 w-full"
              placeholder="e.g. Scottish"
              bind:value={accent}
            /></label
          >
          <div class="grid grid-cols-2 gap-2">
            <label class="filter-label"
              >Presentation<select
                class="input mt-1 w-full"
                bind:value={category}
                ><option value="">Any</option
                >{#each page?.taxonomy.voice_category ?? [] as value}<option
                    {value}>{voiceFacetLabel(value)}</option
                  >{/each}</select
              ></label
            ><label class="filter-label"
              >Pitch<select class="input mt-1 w-full" bind:value={pitch}
                ><option value="">Any</option
                >{#each page?.taxonomy.pitch ?? [] as value}<option {value}
                    >{voiceFacetLabel(value)}</option
                  >{/each}</select
              ></label
            >
          </div>
          <label class="filter-label"
            >Texture<select class="input mt-1 w-full" bind:value={texture}
              ><option value="">Any texture</option
              >{#each page?.taxonomy.textures ?? [] as value}<option {value}
                  >{voiceFacetLabel(value)}</option
                >{/each}</select
            ></label
          >
          <label class="filter-label"
            >Suited to<select class="input mt-1 w-full" bind:value={useCase}
              ><option value="">Any use</option
              >{#each page?.taxonomy.use_cases ?? [] as value}<option {value}
                  >{voiceFacetLabel(value)}</option
                >{/each}</select
            ></label
          >
          <details
            class="border-t border-[var(--line)] pt-3"
            open={Boolean(initialService)}
          >
            <summary class="cursor-pointer text-sm font-semibold"
              >Renderer compatibility</summary
            >
            <div class="mt-3 space-y-3">
              <label class="filter-label"
                >Service<select
                  class="input mt-1 w-full"
                  bind:value={service}
                  onchange={() => (model = '')}
                  ><option value="">Any service</option
                  >{#each services as item}<option value={item.id}
                      >{item.name}</option
                    >{/each}</select
                ></label
              ><label class="filter-label"
                >Model<select
                  class="input mt-1 w-full"
                  bind:value={model}
                  disabled={!service}
                  ><option value="">Any model</option
                  >{#each selectedService?.models ?? [] as item}<option
                      value={item}>{item}</option
                    >{/each}</select
                ></label
              ><label class="flex items-center gap-2 text-sm"
                ><input type="checkbox" bind:checked={readyOnly} />Ready to
                generate</label
              >
            </div>
          </details>
          <label class="flex items-center gap-2 text-sm"
            ><input type="checkbox" bind:checked={reviewedOnly} />Reviewed
            traits only</label
          >
          <button class="btn w-full" onclick={clearFilters}
            >Clear filters</button
          ><button
            class="btn btn-primary w-full catalog-mobile-only"
            onclick={() => (filtersOpen = false)}
            >Show {page?.total ?? 0} voices</button
          >
        </div>
      </div>
      {#if filtersOpen}<button
          class="filter-scrim catalog-mobile-only"
          aria-label="Dismiss filters"
          onclick={() => (filtersOpen = false)}
        ></button>{/if}
      <main class="min-w-0">
        <div class="mb-4 flex flex-wrap items-center justify-between gap-3">
          <p class="muted flex items-center gap-2 text-sm" role="status">
            {#if loading}<LoaderCircle
                size={15}
                class="animate-spin"
              />{/if}{page?.total ?? '…'} voices{collectionId
              ? ' in this collection'
              : ''}
          </p>
          {#if compare.length}<a
              class="btn btn-secondary btn-sm"
              href="#voice-comparison">Compare {compare.length} voices</a
            >{/if}
          <label class="flex items-center gap-2 text-xs"
            >Sort<select class="input text-sm" bind:value={sort}
              ><option value="relevance">Best match</option><option value="name"
                >Name</option
              ><option value="recently_added">Recently added</option><option
                value="recently_updated">Recently updated</option
              ></select
            ></label
          >
        </div>
        {#if filterCount}<div class="mb-4 flex flex-wrap gap-2">
            {#each [language.toUpperCase(), accent, category && voiceFacetLabel(category), pitch && `${voiceFacetLabel(pitch)} pitch`, useCase && voiceFacetLabel(useCase), texture, readyOnly && 'Ready', reviewedOnly && 'Reviewed'].filter(Boolean) as filter}<span
                class="catalog-chip">{filter}</span
              >{/each}
          </div>{/if}
        <div class="space-y-3" aria-busy={loading}>
          {#each page?.items ?? [] as voice (voice.key)}
            <article
              class="surface rounded-2xl border border-[var(--line)] p-4 sm:p-5"
            >
              <div class="flex items-start justify-between gap-3">
                <div class="min-w-0">
                  <button
                    class="break-words text-left text-lg font-semibold hover:text-[var(--accent)]"
                    onclick={(event) => openDetail(voice, event.currentTarget)}
                    >{voice.name}</button
                  >
                  <p class="muted mt-1 line-clamp-2 text-sm">
                    {voice.description ||
                      (voice.kind === 'managed'
                        ? 'Reusable reference voice'
                        : 'Provider speaker')}
                  </p>
                </div>
                <span class="muted shrink-0 text-xs"
                  >{voice.kind === 'managed' ? 'Saved' : 'Provider'}</span
                >
              </div>
              <p class="muted mt-2 break-words text-xs">
                {rendererLabel(voice)}{voice.compatibility.some((c) => c.ready)
                  ? ' · Ready'
                  : ' · Needs setup'}
              </p>
              <div class="mt-3 flex flex-wrap gap-2">
                {#each voice.profile.languages.slice(0, 2) as lang}<span
                    class="catalog-chip"
                    >{lang.language.toUpperCase()}{lang.accent
                      ? ` · ${lang.accent}`
                      : ''}{lang.evidence?.status === 'requested'
                      ? ' · requested'
                      : ''}</span
                  >{/each}{#each traits(voice) as trait}<span
                    class="catalog-chip">{trait}</span
                  >{/each}
              </div>
              <div
                class="mt-4 flex flex-wrap items-center justify-between gap-3"
              >
                <label class="muted flex min-h-10 items-center gap-2 text-xs"
                  ><input
                    type="checkbox"
                    checked={compare.some((v) => v.key === voice.key)}
                    disabled={auditionBusy ||
                      (compare.length >= 3 &&
                        !compare.some((v) => v.key === voice.key))}
                    onchange={() => toggleCompare(voice)}
                  />Compare</label
                >
                <div class="flex flex-wrap gap-2">
                  {#if voice.preview_artifact_id}<button
                      class="btn btn-secondary btn-sm"
                      aria-expanded={playing === voice.key}
                      onclick={() =>
                        (playing = playing === voice.key ? '' : voice.key)}
                      ><Play size={14} />Listen</button
                    >{/if}<button
                    class="btn btn-secondary btn-sm"
                    onclick={(event) => openDetail(voice, event.currentTarget)}
                    >Details</button
                  >{#if onselect || onvoicepublished}<button
                      class="btn btn-sm btn-primary"
                      onclick={() => selectVoice(voice)}>Use voice</button
                    >{/if}
                </div>
              </div>
              {#if playing === voice.key && voice.preview_artifact_id}<div
                  class="mt-3"
                >
                  <AudioPlayer
                    src={`/api/v1/artifacts/${voice.preview_artifact_id}/content`}
                    label={`${voice.name} sample`}
                  />
                </div>{/if}
            </article>
          {:else}
            <div
              class="rounded-2xl border border-dashed border-[var(--line)] px-5 py-12 text-center"
            >
              <Library size={28} class="muted mx-auto" />
              <h3 class="mt-3 text-lg font-semibold">
                {loading ? 'Finding voices…' : 'No voices match yet'}
              </h3>
              {#if !loading}<p class="muted mx-auto mt-2 max-w-sm text-sm">
                  Try fewer filters, add a voice to this collection, or design
                  one for the role.
                </p>
                <div class="mt-5 flex flex-wrap justify-center gap-2">
                  <button class="btn btn-secondary" onclick={clearFilters}
                    >Clear filters</button
                  ><button
                    class="btn btn-primary"
                    onclick={() => (designerOpen = true)}>Design a voice</button
                  >
                </div>{/if}
            </div>
          {/each}
        </div>
        {#if page?.next_cursor}<button
            class="btn mt-5 w-full"
            disabled={loading}
            onclick={() => void load(page?.next_cursor ?? undefined)}
            >Load more voices</button
          >{/if}
      </main>
    </div>
    {#if compare.length}<section
        id="voice-comparison"
        class="surface mt-6 rounded-2xl border border-[var(--line)] p-4 sm:p-6"
      >
        <div class="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h2 class="font-semibold">Compare {compare.length} voices</h2>
            <p class="muted mt-1 text-xs">
              Use the same passage and renderer to hear the difference.
            </p>
          </div>
          <button
            class="btn btn-secondary btn-sm"
            disabled={auditionBusy}
            onclick={() => (compare = [])}>Clear shortlist</button
          >
        </div>
        <label class="mt-4 block text-sm font-semibold"
          >Audition passage<textarea
            class="input mt-1 w-full"
            rows="2"
            maxlength="1000"
            bind:value={auditionText}
            disabled={auditionBusy}></textarea></label
        >{#if !service || !model}<p class="muted mt-2 text-xs">
            Choose a service and model under Renderer compatibility to generate
            auditions.
          </p>{/if}
        <div class="mt-4 flex flex-wrap gap-2">
          {#if auditionBusy}<button
              class="btn btn-secondary"
              onclick={cancelAuditions}>Cancel auditions</button
            >{:else}<button
              class="btn btn-primary"
              disabled={!service || !model || !auditionText.trim()}
              onclick={auditions}
              ><Play size={15} />Generate {compare.length} auditions</button
            >{/if}
        </div>
        <div class="mt-4 grid gap-3 md:grid-cols-3">
          {#each compare as voice}<div
              class="min-w-0 rounded-xl border border-[var(--line)] p-3"
            >
              <p class="mb-3 break-words text-sm font-semibold">{voice.name}</p>
              {#if comparisonAudio[voice.key]?.context === comparisonContext}<AudioPlayer
                  src={`/api/v1/artifacts/${comparisonAudio[voice.key].artifactId}/content`}
                  label={`${voice.name} audition`}
                />{:else}<p class="muted text-xs">
                  {auditionBusy
                    ? 'Preparing audition…'
                    : 'No comparison audition yet'}
                </p>{/if}
            </div>{/each}
        </div>
      </section>{/if}
  {/if}
</div>

{#if designerOpen}<VoiceDesignDialog
    {services}
    {voices}
    initialAccent={accent}
    initialPitch={pitch}
    initialCategory={category}
    initialPrompt={[
      query,
      accent && `${accent} accent`,
      category,
      pitch && `${pitch} pitch`,
      useCase && voiceFacetLabel(useCase)
    ]
      .filter(Boolean)
      .join(', ')}
    onclose={() => (designerOpen = false)}
    onsaved={async (voiceId, _providerVoiceId, warning) => {
      const targetCollectionId = collectionId;
      const warnings = warning ? [warning] : [];
      designerOpen = false;
      try {
        await refreshAuxiliary();
        const result = await voiceLibraryApi.query({
          query: voiceId,
          kind: 'managed',
          limit: 1
        });
        const saved = result.items.find((v) => v.id === voiceId);
        const group = collections.find((c) => c.id === targetCollectionId);
        if (saved && group) {
          try {
            const updated = await voiceLibraryApi.membership(
              group,
              saved.reference,
              true
            );
            saved.collections = [
              ...saved.collections.filter((c) => c.id !== updated.id),
              { id: updated.id, name: updated.name }
            ];
          } catch (caught) {
            warnings.push(
              `The voice was saved, but could not be added to ${group.name}: ${errorMessage(caught)}`
            );
          }
        }
        await refreshAuxiliary();
        await load();
        if (saved) openDetail(saved);
        notice = warnings.join(' ');
      } catch (caught) {
        error = `The voice was saved, but the library could not refresh: ${errorMessage(caught)}`;
      }
    }}
  />{/if}
{#if pendingClose}<div
    class="fixed inset-0 z-[90] grid place-items-center bg-black/40 p-4"
  >
    <div
      use:modalFocus={{ onclose: () => (pendingClose = null) }}
      role="dialog"
      aria-modal="true"
      aria-labelledby="voice-unsaved-title"
      class="surface w-full max-w-md rounded-2xl p-5"
    >
      <h2 id="voice-unsaved-title" class="text-lg font-semibold">
        Keep your profile changes?
      </h2>
      <p class="muted mt-2 text-sm">
        Save the voice profile or discard the draft before leaving.
      </p>
      <div class="mt-5 flex flex-wrap justify-end gap-2">
        <button class="btn btn-secondary" onclick={() => (pendingClose = null)}
          >Keep editing</button
        ><button
          class="btn btn-secondary"
          onclick={() => {
            const close = pendingClose;
            pendingClose = null;
            editing = false;
            close?.();
          }}>Discard</button
        ><button
          class="btn btn-primary"
          disabled={saving}
          onclick={async () => {
            if (await save()) {
              const close = pendingClose;
              pendingClose = null;
              close?.();
            }
          }}>Save and continue</button
        >
      </div>
    </div>
  </div>{/if}

<style>
  .catalog-library :global(.input) {
    min-width: 0;
    max-width: 100%;
    min-height: 2.65rem;
    border: 1px solid var(--line);
    border-radius: 0.65rem;
    background: var(--paper);
    padding: 0.55rem 0.7rem;
    font-size: 0.875rem;
    font-weight: 400;
  }
  .catalog-library .catalog-mobile-only {
    display: none;
  }
  .catalog-toolbar {
    display: grid;
    grid-template-columns: minmax(12rem, 1fr) minmax(10rem, 14rem) auto;
    gap: 0.75rem;
    align-items: stretch;
  }
  .catalog-layout {
    display: grid;
    grid-template-columns: 15rem minmax(0, 1fr);
    gap: 1.5rem;
  }
  .catalog-filters {
    align-self: start;
    padding: 1.1rem;
    border: 1px solid var(--line);
    border-radius: 1rem;
    background: var(--paper-strong);
  }
  .filter-label {
    display: block;
    font-size: 0.75rem;
    font-weight: 600;
  }
  .catalog-chip {
    display: inline-flex;
    align-items: center;
    padding: 0.3rem 0.55rem;
    border-radius: 0.5rem;
    background: var(--accent-soft);
    color: var(--ink);
    font-size: 0.72rem;
    overflow-wrap: anywhere;
  }
  @media (max-width: 1023px) {
    .catalog-library .catalog-mobile-only {
      display: inline-flex;
    }
    .catalog-toolbar {
      grid-template-columns: auto minmax(0, 1fr) auto;
    }
    .search-field {
      grid-column: 1 / -1;
    }
    .catalog-layout {
      display: block;
    }
    .catalog-filters {
      display: none;
    }
    .catalog-filters.filters-open {
      display: block;
      position: fixed;
      z-index: 82;
      inset: 0 0 0 auto;
      width: min(23rem, 92vw);
      overflow-y: auto;
      border-radius: 0;
      padding: 1.25rem;
      box-shadow: -10px 0 40px #0002;
    }
    .filter-scrim {
      position: fixed;
      z-index: 81;
      inset: 0;
      background: #0006;
    }
  }
</style>
