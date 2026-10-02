<script lang="ts">
  import type { SubtitleLanguageProfile } from './api-models';
  import { translationLanguageName } from './translation-project-display';

  let {
    automatic,
    profiles,
    chars,
    cps,
    lines
  }: {
    automatic: boolean;
    profiles?: Record<string, SubtitleLanguageProfile | null>;
    chars: number;
    cps: number;
    lines: number;
  } = $props();
  const tracks = $derived(
    Object.entries(profiles ?? {}).filter(([, profile]) => profile)
  );
</script>

<div
  class="muted mt-2 space-y-1 text-xs"
  data-subtitle-profile-summary
  aria-live="polite"
>
  {#if automatic}
    <p>Automatic limits follow each subtitle track’s language.</p>
    {#each tracks as [track, profile]}
      {#if profile}
        <p>
          {track === 'target' ? 'Translation' : 'Source'} ·
          {['', 'auto', 'und', 'unknown'].includes(profile.language)
            ? 'Language not yet detected'
            : translationLanguageName(profile.language)}:
          {profile.limits.max_chars_per_line.effective} units per line,
          {lines}
          {lines === 1 ? 'line' : 'lines'} maximum,
          {profile.limits.max_chars_per_second.effective} units/second target.
        </p>
      {/if}
    {/each}
  {:else}
    <p>
      Custom: {chars} units per line, {lines}
      {lines === 1 ? 'line' : 'lines'} maximum, {cps} units/second target. Language
      changes keep these limits.
    </p>
  {/if}
  <p>
    Reading speed is a target; dense speech and fixed timing can exceed it. CJK
    full-width characters count as one unit, half-width characters as half a
    unit.
  </p>
</div>
