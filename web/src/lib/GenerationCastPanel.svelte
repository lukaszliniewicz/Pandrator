<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import { beforeNavigate, goto } from '$app/navigation';
  import { modalFocus } from './modal-focus';
  import { apiJson } from './api';
  import {
    getTtsCatalogue,
    getVoiceLibrary
  } from './tts-catalogue-cache';
  import { errorMessage } from './errors';
  import type { VoiceRecord, TtsService } from './api-models';
  import {
    voiceCategories,
    type Character,
    type Cast,
    type GenerationControls,
    type VoiceBinding
  } from './generation-controls';
  import CastVoiceField from './CastVoiceField.svelte';
  let {
    sessionId,
    service = '',
    model = '',
    busy = false,
    standalone = false,
    navigationManaged = false,
    sessionVoice = '',
    onchanged
  }: {
    sessionId: string;
    service?: string;
    model?: string;
    busy?: boolean;
    standalone?: boolean;
    navigationManaged?: boolean;
    sessionVoice?: string;
    onchanged?: (characters: Character[]) => void;
  } = $props();
  let loaded = $state(false);
  let pending = $state(false);
  let error = $state('');
  let message = $state('');
  let revision = $state(0);
  let characters = $state<Character[]>([]);
  let cast = $state<Cast>({
    narrator: null,
    categories: {},
    characters: {},
    source_speakers: {}
  });
  let baseline = $state('');
  let unlockIds = $state<string[]>([]);
  let voices = $state<VoiceRecord[]>([]);
  let sourceLabel = $state('');
  let expandedCharacters = $state<string[]>([]);
  let pendingLeave = $state<(() => void) | null>(null);
  let allowLeave = false;
  let services = $state<TtsService[]>([]);
  const selectedService = $derived(
    services.find((item) => item.id === service || item.name === service)
  );
  const suggestions = $derived(
    selectedService?.voice_catalogues?.[model] ??
      selectedService?.live_voices ??
      selectedService?.voices ??
      []
  );
  const rendererId = $derived(selectedService?.id ?? service);
  const inheritedSession = $derived(
    sessionVoice ? { voice: sessionVoice, service: rendererId, model } : null
  );
  onMount(() => {
    if (standalone) void load();
  });
  beforeNavigate((event) => {
    if (!standalone || navigationManaged || allowLeave || !dirty) return;
    event.cancel();
    if (!event.willUnload && event.to?.url) {
      const destination = event.to.url.href;
      pendingLeave = () => {
        allowLeave = true;
        void goto(destination);
      };
    }
  });
  let alive = true;
  onDestroy(() => {
    alive = false;
  });
  const dirty = $derived(
    loaded && JSON.stringify({ characters, cast }) !== baseline
  );
  const blocked = $derived(busy || pending);
  const path = $derived(
    `/sessions/${encodeURIComponent(sessionId)}/generation-controls`
  );
  export function draftState() {
    return {
      dirty,
      blocked,
      valid: characters.every((item) => Boolean(item.display_name.trim()))
    };
  }
  export async function saveChanges() {
    return save();
  }
  export function discardChanges() {
    if (!baseline) return;
    const saved = JSON.parse(baseline) as {
      characters: Character[];
      cast: Cast;
    };
    characters = saved.characters;
    cast = saved.cast;
    unlockIds = [];
    error = '';
    message = '';
  }
  async function load(force = false) {
    pending = true;
    error = '';
    try {
      const [result, library, catalogue] = await Promise.all([
        apiJson<GenerationControls>(path),
        getVoiceLibrary(force),
        getTtsCatalogue(false, force)
      ]);
      if (!alive) return;
      voices = library.items;
      services = catalogue.services;
      if (!dirty) {
        revision = result.revision;
        characters = result.characters;
        cast = {
          narrator: result.cast.narrator ?? null,
          categories: result.cast.categories ?? {},
          characters: result.cast.characters ?? {},
          source_speakers: result.cast.source_speakers ?? {}
        };
        baseline = JSON.stringify({ characters, cast });
        unlockIds = [];
        loaded = true;
      } else if (revision !== result.revision)
        message =
          'The saved dictionary changed elsewhere. Your draft is preserved; discard it to load the newer version.';
      onchanged?.(result.characters);
    } catch (caught) {
      if (alive) error = errorMessage(caught);
    } finally {
      if (alive) pending = false;
    }
  }
  function bindVoice(
    group: 'categories' | 'characters' | 'source_speakers',
    key: string,
    value: VoiceBinding | null
  ) {
    const entries = { ...cast[group] };
    if (value) entries[key] = value;
    else delete entries[key];
    cast = { ...cast, [group]: entries };
  }
  function addCharacter() {
    const id = `c-${crypto.randomUUID()}`;
    expandedCharacters = [...expandedCharacters, id];
    characters = [
      ...characters,
      {
        id,
        display_name: '',
        aliases: [],
        voice_category: 'unspecified',
        notes: '',
        locked: false,
        status: 'accepted',
        origin: 'manual'
      }
    ];
  }
  function removeCharacter(character: Character) {
    characters = characters.filter((item) => item.id !== character.id);
    bindVoice('characters', character.id, null);
  }
  async function save() {
    if (!draftState().valid) return false;
    pending = true;
    error = '';
    message = '';
    try {
      const result = await apiJson<GenerationControls>(path, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          expected_revision: revision,
          characters,
          cast,
          unlock_ids: unlockIds
        })
      });
      if (!alive) return false;
      revision = result.revision;
      characters = result.characters;
      cast = result.cast;
      unlockIds = [];
      baseline = JSON.stringify({ characters, cast });
      message =
        'Characters and cast saved. New generation requests use these assignments.';
      onchanged?.(characters);
      return true;
    } catch (caught) {
      if (alive) error = errorMessage(caught);
      return false;
    } finally {
      if (alive) pending = false;
    }
  }
</script>

<svelte:window
  onbeforeunload={(event) => {
    if (dirty) event.preventDefault();
  }}
/>

<details
  open={standalone}
  class="speech-controls border-t border-[var(--line)] pt-4 sm:rounded-xl sm:border sm:p-4"
  ontoggle={(event) => {
    if (event.currentTarget.open && !loaded && !pending) void load();
  }}
>
  <summary class="cursor-pointer font-semibold"
    >Characters and cast <span class="muted ml-2 text-xs"
      >Narrator · characters · dialogue defaults</span
    ></summary
  >
  <div class="mt-4 space-y-4">
    <p class="muted text-sm">
      A character keeps the same identity across the book. Names and aliases
      help recognition; casting decides how that character sounds. Unknown is
      separate from androgynous.
    </p>
    {#if error}<p role="alert" class="text-sm text-red-700">{error}</p>{/if}
    {#if message}<p role="status" class="text-sm muted">{message}</p>{/if}
    {#if loaded}
      <div class="grid gap-4 sm:grid-cols-2">
        <CastVoiceField
          label="Narrator voice"
          value={cast.narrator}
          inherited={inheritedSession}
          {voices}
          {suggestions}
          service={rendererId}
          {model}
          disabled={blocked}
          onchange={(value) => (cast = { ...cast, narrator: value })}
        />
        <p class="muted text-xs self-center">
          Dialogue uses an assigned character voice, a source speaker
          assignment, or a dialogue fallback. Remaining parts use the narrator,
          then the session voice. All voices in a run use its selected service
          and model.
        </p>
      </div>
      <div
        class="flex flex-wrap items-center justify-between gap-2 border-t border-[var(--line)] pt-4"
      >
        <h4 class="font-semibold">
          Character roster <span class="muted text-xs"
            >{characters.length}
            {characters.length === 1 ? 'entry' : 'entries'}</span
          >
        </h4>
        <button class="btn" disabled={blocked} onclick={addCharacter}
          >Add character</button
        >
      </div>
      {#if !characters.length}<p class="muted text-sm">
          Add a named character here, or review characters proposed during
          speech optimization.
        </p>{/if}
      {#each characters as character (character.id)}
        {@const protectedEntry =
          character.locked && !unlockIds.includes(character.id)}
        <article class="rounded-xl border border-[var(--line)] p-3 space-y-3">
          <div class="flex flex-wrap items-center justify-between gap-2">
            <h4 class="font-semibold">
              {character.display_name || 'New character'}
            </h4>
            <span class="muted text-xs"
              >{character.status === 'proposed'
                ? 'Proposed · review identity'
                : character.voice_category === 'unspecified'
                  ? 'Presentation unspecified'
                  : character.voice_category}{protectedEntry
                ? ' · protected'
                : ''}</span
            >
          </div>
          <CastVoiceField
            label={`Voice for ${character.display_name || 'this character'}`}
            value={cast.characters[character.id]}
            inherited={cast.categories[character.voice_category] ??
              cast.narrator ??
              inheritedSession}
            inheritedLabel={cast.categories[character.voice_category]
              ? `${character.voice_category === 'unspecified' ? 'Unknown speaker' : character.voice_category} dialogue default`
              : cast.narrator
                ? 'Narrator'
                : 'Session voice'}
            {voices}
            {suggestions}
            service={rendererId}
            {model}
            disabled={blocked}
            onchange={(value) => bindVoice('characters', character.id, value)}
          />
          <details
            open={expandedCharacters.includes(character.id)}
            ontoggle={(event) => {
              expandedCharacters = event.currentTarget.open
                ? [...new Set([...expandedCharacters, character.id])]
                : expandedCharacters.filter((id) => id !== character.id);
            }}
            class="border-t border-[var(--line)] pt-2"
          >
            <summary class="cursor-pointer py-2 text-sm"
              >Edit identity, aliases &amp; protection</summary
            >
            <div class="mt-3 space-y-3">
              <div class="flex flex-wrap justify-between gap-2">
                <span class="muted text-xs"
                  >{character.status === 'proposed'
                    ? 'Proposed · needs review'
                    : 'Accepted'} · {character.id}</span
                >
                {#if protectedEntry}<button
                    class="btn text-xs"
                    disabled={blocked}
                    onclick={() => (unlockIds = [...unlockIds, character.id])}
                    >Unlock for editing</button
                  >{:else}<button
                    class="btn text-xs"
                    disabled={blocked}
                    onclick={() => removeCharacter(character)}
                    >Remove character</button
                  >{/if}
              </div>
              <fieldset
                disabled={blocked || protectedEntry}
                class="grid gap-3 sm:grid-cols-2"
              >
                <label class="text-sm"
                  >Name<input
                    class="input mt-1 w-full"
                    maxlength="255"
                    bind:value={character.display_name}
                  /></label
                >
                <label class="text-sm"
                  >Voice category<select
                    class="input mt-1 w-full"
                    bind:value={character.voice_category}
                    >{#each voiceCategories as category}<option value={category}
                        >{category}</option
                      >{/each}</select
                  ></label
                >
                <label class="text-sm sm:col-span-2"
                  >Aliases <span class="muted text-xs"
                    >Separate with commas</span
                  ><input
                    class="input mt-1 w-full"
                    value={character.aliases.join(', ')}
                    onchange={(event) =>
                      (character.aliases = event.currentTarget.value
                        .split(',')
                        .map((item) => item.trim())
                        .filter(Boolean))}
                  /></label
                >
                <label class="text-sm sm:col-span-2"
                  >Identity notes<textarea
                    class="input mt-1 w-full"
                    rows="2"
                    maxlength="2000"
                    bind:value={character.notes}></textarea></label
                >
                <label class="flex items-center gap-2 text-sm"
                  ><input
                    type="checkbox"
                    bind:checked={character.locked}
                  />Protect this identity</label
                >
                {#if character.status === 'proposed'}<button
                    class="btn"
                    onclick={() => (character.status = 'accepted')}
                    >Accept identity</button
                  >{/if}
              </fieldset>
            </div>
          </details>
        </article>
      {/each}
      <details class="rounded-xl border border-[var(--line)] p-3">
        <summary class="cursor-pointer text-sm font-semibold"
          >Dialogue fallback voices</summary
        >
        <p class="muted my-3 text-xs">
          Used when a dialogue speaker has no individual assignment. Unknown
          remains separate from androgynous.
        </p>
        <div class="grid gap-4 sm:grid-cols-2">
          {#each voiceCategories as category}
            <CastVoiceField
              label={`${category === 'unspecified' ? 'Unknown speaker' : category[0].toUpperCase() + category.slice(1)} dialogue default`}
              value={cast.categories[category]}
              inherited={cast.narrator ?? inheritedSession}
              inheritedLabel={cast.narrator ? 'Narrator' : 'Session voice'}
              {voices}
              {suggestions}
              service={rendererId}
              {model}
              disabled={blocked}
              onchange={(value) => bindVoice('categories', category, value)}
            />
          {/each}
        </div>
      </details>
      <details class="text-sm">
        <summary class="cursor-pointer">Source speaker assignments</summary>
        <div class="mt-3 space-y-3">
          {#each Object.keys(cast.source_speakers) as speaker}<CastVoiceField
              label={`Source speaker ${speaker}`}
              value={cast.source_speakers[speaker]}
              {voices}
              {suggestions}
              service={rendererId}
              {model}
              disabled={blocked}
              onchange={(value) => bindVoice('source_speakers', speaker, value)}
            />{/each}
          <label class="block"
            >Source speaker label<input
              class="input mt-1 w-full"
              bind:value={sourceLabel}
              placeholder="SPEAKER_01"
            /></label
          >
          <CastVoiceField
            label="Assign a voice to this source label"
            {voices}
            {suggestions}
            service={rendererId}
            {model}
            disabled={blocked || !sourceLabel.trim()}
            onchange={(value) => {
              if (value) {
                bindVoice('source_speakers', sourceLabel.trim(), value);
                sourceLabel = '';
              }
            }}
          />
        </div>
      </details>
      <div class="flex flex-wrap items-center gap-2">
        <button
          class="btn btn-primary"
          disabled={blocked ||
            !dirty ||
            characters.some((item) => !item.display_name.trim())}
          onclick={() => void save()}>Save characters and cast</button
        ><button class="btn" disabled={blocked} onclick={() => void load(true)}
          >Refresh cast</button
        >{#if dirty}<span class="text-xs text-amber-700"
            >Unsaved character or cast changes</span
          ><button
            class="btn"
            disabled={blocked}
            onclick={() => {
              loaded = false;
              baseline = '';
              void load();
            }}>Discard cast changes</button
          >{/if}
      </div>
    {:else}<p class="muted text-sm">
        {pending
          ? 'Loading characters and voices…'
          : 'Open this panel to load the dictionary.'}
      </p>{/if}
  </div>
</details>

{#if pendingLeave}<div
    class="fixed inset-0 z-[90] grid place-items-center bg-black/45 p-4"
  >
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="cast-unsaved-title"
      use:modalFocus={{ onclose: () => (pendingLeave = null) }}
      class="compact-confirmation surface w-full max-w-md rounded-2xl p-5"
    >
      <h2 id="cast-unsaved-title" class="text-lg font-semibold">
        Keep your cast changes?
      </h2>
      <p class="muted mt-2 text-sm">
        Save your character identities and voice assignments before leaving.
      </p>
      <div class="mt-5 flex flex-wrap justify-end gap-2">
        <button class="btn btn-secondary" onclick={() => (pendingLeave = null)}
          >Keep editing</button
        ><button
          class="btn btn-secondary"
          disabled={blocked}
          onclick={() => {
            const leave = pendingLeave;
            pendingLeave = null;
            discardChanges();
            leave?.();
          }}>Discard</button
        ><button
          class="btn btn-primary"
          disabled={blocked || !draftState().valid}
          onclick={async () => {
            if (await save()) {
              const leave = pendingLeave;
              pendingLeave = null;
              leave?.();
            }
          }}>Save and continue</button
        >
      </div>
    </div>
  </div>{/if}
