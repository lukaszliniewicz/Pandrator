<script lang="ts">
  import { LANGUAGE_OPTIONS } from './settings-fields';
  import { voiceLanguageName } from './voice-presentation';
  import { Plus, X } from '@lucide/svelte';
  import {
    voiceFacetLabel,
    type VoiceProfile,
    type VoiceLanguage
  } from './voice-library-api';
  let {
    profile = $bindable(),
    disabled = false,
    evidenceArtifactId = ''
  }: {
    profile: VoiceProfile;
    disabled?: boolean;
    evidenceArtifactId?: string;
  } = $props();
  const groups = [
    {
      key: 'textures' as const,
      label: 'Texture',
      values: [
        'warm',
        'bright',
        'dark',
        'airy',
        'breathy',
        'raspy',
        'gravelly',
        'resonant',
        'clear',
        'nasal'
      ]
    },
    {
      key: 'delivery_presets' as const,
      label: 'Delivery presets',
      values: [
        'neutral',
        'conversational',
        'formal',
        'storytelling',
        'dramatic'
      ]
    },
    {
      key: 'use_cases' as const,
      label: 'Suited to',
      values: [
        'audiobook_narration',
        'character_dialogue',
        'voiceover',
        'documentary',
        'news',
        'advertising',
        'instructional'
      ]
    }
  ];
  function changed(field: string) {
    profile.evidence = {
      ...profile.evidence,
      [field]: { source: 'user', status: 'described' }
    };
  }
  function toggle(
    field: 'textures' | 'delivery_presets' | 'use_cases',
    value: string
  ) {
    profile[field] = profile[field].includes(value)
      ? profile[field].filter((v) => v !== value)
      : [...profile[field], value];
    changed(field);
  }
  function languageChanged(index: number) {
    profile.languages[index].evidence = { source: 'user', status: 'described' };
  }
  function addLanguage() {
    profile.languages = [
      ...profile.languages,
      {
        language: '',
        accent: '',
        evidence: { source: 'user', status: 'described' }
      } satisfies VoiceLanguage
    ];
  }
</script>

<div class="space-y-5">
  <div class="grid gap-3 sm:grid-cols-2">
    <label class="text-sm font-semibold"
      >Pitch
      <select
        class="input mt-1 w-full"
        bind:value={profile.pitch}
        {disabled}
        onchange={() => changed('pitch')}
      >
        <option value={null}>Not described</option><option value="low"
          >Low</option
        ><option value="mid">Mid</option><option value="high">High</option>
      </select>
    </label>
    <label class="text-sm font-semibold"
      >Perceived age
      <select
        class="input mt-1 w-full"
        bind:value={profile.perceived_age}
        {disabled}
        onchange={() => changed('perceived_age')}
      >
        <option value={null}>Not described</option
        >{#each ['childlike', 'youthful', 'adult', 'older'] as age}<option
            value={age}>{voiceFacetLabel(age)}</option
          >{/each}
      </select>
    </label>
  </div>
  {#each groups as group}
    <fieldset {disabled}>
      <legend class="mb-2 text-sm font-semibold">{group.label}</legend>
      <div class="flex flex-wrap gap-2">
        {#each group.values as value}<button
            type="button"
            aria-pressed={profile[group.key].includes(value)}
            onclick={() => toggle(group.key, value)}
            class="trait-chip"
            class:chosen={profile[group.key].includes(value)}
            >{voiceFacetLabel(value)}</button
          >{/each}
      </div>
    </fieldset>
  {/each}
  <fieldset {disabled} class="space-y-3">
    <legend class="mb-1 text-sm font-semibold">Languages and accents</legend>
    <p class="muted text-xs">
      Describe what this voice demonstrates. The renderer's supported languages
      are shown separately.
    </p>
    {#each profile.languages as language, index}
      <div class="rounded-xl border border-[var(--line)] p-3">
        <div class="grid grid-cols-[1fr_auto] items-start gap-2">
          <div class="grid gap-3 sm:grid-cols-2">
            <label class="text-xs font-semibold"
              >Language<select
                class="input mt-1 w-full"
                value={LANGUAGE_OPTIONS.some(
                  (item) => item.value === language.language
                )
                  ? language.language
                  : 'custom'}
                onchange={(event) => {
                  language.language =
                    event.currentTarget.value === 'custom'
                      ? ''
                      : event.currentTarget.value;
                  languageChanged(index);
                }}
              >
                <option value="custom">Other language or locale…</option>
                {#each LANGUAGE_OPTIONS.filter((item) => item.value !== 'auto') as item}<option
                    value={item.value}>{item.label} ({item.value})</option
                  >{/each}
              </select>
              {#if !LANGUAGE_OPTIONS.some((item) => item.value === language.language)}<input
                  aria-label="Language code"
                  class="input mt-2 w-full"
                  placeholder="e.g. en-GB"
                  maxlength="40"
                  bind:value={language.language}
                  oninput={() => languageChanged(index)}
                /><span class="muted mt-1 block font-normal"
                  >{language.language
                    ? voiceLanguageName(language.language)
                    : 'Enter a language or locale code.'}</span
                >{/if}
            </label>
            <label class="text-xs font-semibold"
              >Accent<input
                class="input mt-1 w-full"
                placeholder="e.g. Scottish"
                maxlength="80"
                bind:value={language.accent}
                oninput={() => languageChanged(index)}
              /></label
            >
          </div>
          <button
            type="button"
            class="btn btn-icon mt-5"
            aria-label={`Remove language ${index + 1}`}
            onclick={() =>
              (profile.languages = profile.languages.filter(
                (_, i) => i !== index
              ))}><X size={16} /></button
          >
        </div>
        <p class="muted mt-2 text-xs">
          {language.evidence?.status === 'reviewed'
            ? 'Reviewed by listening'
            : language.evidence?.status === 'requested'
              ? 'Requested in a design brief · needs an audition'
              : 'Described · not confirmed by an audition'}
        </p>
        {#if evidenceArtifactId}<label
            class="mt-3 flex items-start gap-2 text-xs"
            ><input
              type="checkbox"
              checked={language.evidence?.status === 'reviewed'}
              onchange={(event) =>
                (language.evidence = event.currentTarget.checked
                  ? {
                      source: 'audition_review',
                      status: 'reviewed',
                      artifact_id: evidenceArtifactId
                    }
                  : { source: 'user', status: 'described' })}
            /><span
              >I listened to this reference and confirmed this language and
              accent.</span
            ></label
          >{/if}
      </div>
    {/each}
    <button
      type="button"
      class="btn btn-sm"
      onclick={addLanguage}
      disabled={disabled || profile.languages.length >= 30}
      ><Plus size={14} />Add language</button
    >
  </fieldset>
  <label class="block text-sm font-semibold"
    >Your tags<input
      class="input mt-1 w-full"
      {disabled}
      value={profile.tags.join(', ')}
      placeholder="winter, gentle, favourite"
      onchange={(event) => {
        profile.tags = [
          ...new Set(
            event.currentTarget.value
              .split(',')
              .map((v) => v.trim())
              .filter(Boolean)
          )
        ];
        changed('tags');
      }}
    /><span class="muted mt-1 block text-xs font-normal"
      >Separate tags with commas. Editing a trait records it as your
      description.</span
    ></label
  >
</div>

<style>
  .trait-chip {
    border: 1px solid var(--line);
    border-radius: 0.65rem;
    padding: 0.45rem 0.7rem;
    font-size: 0.8rem;
    background: var(--paper);
    min-height: 2.4rem;
  }
  .trait-chip.chosen {
    background: var(--accent-soft);
    border-color: var(--accent);
    color: var(--accent);
  }
</style>
