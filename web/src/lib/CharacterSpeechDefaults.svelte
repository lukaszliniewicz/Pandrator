<script lang="ts">
  import { onMount, untrack } from 'svelte';
  import { apiJson } from './api';
  import { sessionApi } from './domain-api';
  import { errorMessage } from './errors';
  import CastVoiceField from './CastVoiceField.svelte';
  import DeliveryFields from './DeliveryFields.svelte';
  import { emptyDelivery, type SpeechDelivery } from './speech-inspector';
  import type { SpeechCapabilities } from './speech-support';
  import type {
    Character,
    GenerationControls,
    VoiceBinding
  } from './generation-controls';
  import { voiceCategories } from './generation-controls';
  let {
    sessionId,
    controls,
    character,
    service,
    model,
    capabilities,
    disabled = false,
    onchanged,
    ondraft
  }: {
    sessionId: string;
    controls: GenerationControls;
    character: Character;
    service: string;
    model: string;
    capabilities?: SpeechCapabilities;
    disabled?: boolean;
    onchanged: (controls: GenerationControls) => void | Promise<void>;
    ondraft: (state: { dirty: boolean; pending: boolean }) => void;
  } = $props();
  let direction = $state<SpeechDelivery>(
    untrack(() => ({ ...emptyDelivery(), ...character.delivery }))
  );
  function identityFromCharacter() {
    return {
      display_name: character.display_name,
      aliases: character.aliases.join(', '),
      voice_category: character.voice_category,
      notes: character.notes,
      status: character.status
    };
  }
  let identity = $state(untrack(identityFromCharacter));
  let voice = $state<VoiceBinding | null>(
    untrack(() =>
      structuredClone(
        $state.snapshot(controls.cast.characters[character.id] ?? null)
      )
    )
  );
  let baseline = $state(
    untrack(() => JSON.stringify({ identity, direction, voice }))
  );
  let pending = $state(false);
  let error = $state('');
  let unlock = $state(false);
  let projectVoice = $state('');
  const dirty = $derived(
    JSON.stringify({ identity, direction, voice }) !== baseline
  );
  const protectedEntry = $derived(character.locked && !unlock);
  const inherited = $derived(
    controls.cast.categories[character.voice_category] ??
      controls.cast.narrator ??
      (projectVoice ? { voice: projectVoice, service, model } : null)
  );
  $effect(() => ondraft({ dirty, pending }));
  onMount(() => {
    let alive = true;
    void sessionApi
      .settings(sessionId, 'tts')
      .then((settings) => {
        if (alive) projectVoice = String(settings.effective.voice ?? '');
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  });
  export function discardChanges() {
    identity = identityFromCharacter();
    direction = { ...emptyDelivery(), ...character.delivery };
    voice = structuredClone(
      $state.snapshot(controls.cast.characters[character.id] ?? null)
    );
    baseline = JSON.stringify({ identity, direction, voice });
    error = '';
  }
  async function save() {
    if (
      pending ||
      disabled ||
      protectedEntry ||
      !dirty ||
      !identity.display_name.trim()
    )
      return;
    pending = true;
    error = '';
    try {
      const nextCast = structuredClone($state.snapshot(controls.cast));
      if (voice) nextCast.characters[character.id] = $state.snapshot(voice);
      else delete nextCast.characters[character.id];
      const saved = await apiJson<GenerationControls>(
        `/sessions/${encodeURIComponent(sessionId)}/generation-controls`,
        {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            expected_revision: controls.revision,
            characters: controls.characters.map((entry) =>
              entry.id === character.id
                ? {
                    ...entry,
                    ...identity,
                    aliases: identity.aliases
                      .split(',')
                      .map((alias) => alias.trim())
                      .filter(Boolean),
                    delivery: direction
                  }
                : entry
            ),
            cast: nextCast,
            unlock_ids: unlock ? [character.id] : []
          })
        }
      );
      baseline = JSON.stringify({ identity, direction, voice });
      await onchanged(saved);
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      pending = false;
    }
  }
</script>

<div class="space-y-4">
  <p class="muted text-xs">
    Keep this speaker’s identity and cast voice consistent throughout the
    project. Changes here apply wherever this speaker is assigned.
  </p>
  {#if protectedEntry}<button
      class="btn btn-sm"
      disabled={disabled || pending}
      onclick={() => (unlock = true)}>Unlock character for this edit</button
    >{/if}
  <fieldset
    disabled={disabled || pending || protectedEntry}
    class="grid gap-3 sm:grid-cols-2"
  >
    <label class="text-sm"
      >Speaker name<input
        class="input mt-1 w-full"
        maxlength="255"
        bind:value={identity.display_name}
      /></label
    >
    <label class="text-sm"
      >Voice presentation<select
        class="input mt-1 w-full"
        bind:value={identity.voice_category}
      >
        {#each voiceCategories as category}<option value={category}
            >{category === 'unspecified' ? 'Unspecified' : category}</option
          >{/each}</select
      ></label
    >
    <label class="text-sm sm:col-span-2"
      >Aliases<input
        class="input mt-1 w-full"
        bind:value={identity.aliases}
        placeholder="Other names, separated by commas"
      /></label
    >
    <label class="text-sm sm:col-span-2"
      >Identity notes<textarea
        class="input mt-1 w-full"
        maxlength="2000"
        rows="2"
        bind:value={identity.notes}></textarea></label
    >
    {#if identity.status === 'proposed'}<button
        class="btn btn-sm"
        onclick={() => (identity.status = 'accepted')}
        >Accept proposed speaker</button
      >{/if}
  </fieldset>
  <p class="muted text-xs">
    Voice presentation is optional and selects a fallback voice. Names and
    aliases identify the speaker.
  </p>
  <CastVoiceField
    label={`Voice for ${character.display_name}`}
    value={voice}
    {inherited}
    inheritedLabel={controls.cast.categories[character.voice_category]
      ? 'Dialogue fallback'
      : controls.cast.narrator
        ? 'Narrator'
        : 'Project voice'}
    {service}
    {model}
    disabled={disabled || pending || protectedEntry}
    onchange={(next) => (voice = next)}
  />
  <DeliveryFields
    value={direction}
    label="Character delivery default"
    {capabilities}
    disabled={disabled || pending || protectedEntry}
    onchange={(field, value) =>
      (direction = {
        ...direction,
        [field]: value,
        ...(field === 'emotion' && !value.trim()
          ? { emotion_intensity: null }
          : {})
      })}
    onintensity={(value) =>
      (direction = { ...direction, emotion_intensity: value })}
  />
  {#if error}<p class="text-sm text-red-700" role="alert">{error}</p>{/if}
  <div class="flex flex-wrap gap-2">
    <button
      class="btn btn-primary btn-sm"
      disabled={disabled ||
        pending ||
        protectedEntry ||
        !dirty ||
        !identity.display_name.trim()}
      onclick={() => void save()}
      >{pending ? 'Saving…' : 'Save speaker and cast'}</button
    >
    {#if dirty}<button
        class="btn btn-sm"
        disabled={pending}
        onclick={discardChanges}>Discard character changes</button
      >{/if}
  </div>
  <p class="muted text-xs">
    Changes affect future audio. Existing recordings stay in take history.
  </p>
</div>
