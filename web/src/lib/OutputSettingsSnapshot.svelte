<script lang="ts">
  import { ChevronDown, SlidersHorizontal } from '@lucide/svelte';

  let { snapshot }: { snapshot: unknown } = $props();

  type SnapshotSection = {
    key: string;
    label: string;
    values: Record<string, unknown>;
  };

  const sectionLabels: Record<string, string> = {
    output: 'Output',
    audio: 'Audio assembly',
    subtitles: 'Subtitles'
  };

  function readSnapshot(value: unknown) {
    if (!value || typeof value !== 'object') return null;
    const record = value as Record<string, unknown>;
    const rawSections = record.sections;
    if (!rawSections || typeof rawSections !== 'object') return null;
    const sections = Object.entries(rawSections as Record<string, unknown>)
      .filter((entry): entry is [string, Record<string, unknown>] => {
        const section = entry[1];
        return Boolean(
          section &&
          typeof section === 'object' &&
          Object.keys(section as Record<string, unknown>).length
        );
      })
      .map(([key, values]) => ({
        key,
        label: sectionLabels[key] ?? humanize(key),
        values
      }));
    return {
      hash:
        typeof record.settings_hash === 'string' ? record.settings_hash : '',
      sections
    };
  }

  function humanize(value: string) {
    return value
      .replaceAll('_', ' ')
      .replace(/\b\w/g, (letter) => letter.toUpperCase())
      .replace(/\bDb\b/g, 'dB')
      .replace(/\bLufs\b/g, 'LUFS')
      .replace(/\bMs\b/g, 'ms');
  }

  function formatValue(value: unknown) {
    if (value === null || value === undefined || value === '') return 'Not set';
    if (typeof value === 'boolean') return value ? 'Yes' : 'No';
    if (Array.isArray(value)) return value.length ? value.join(', ') : 'None';
    if (typeof value === 'object') return JSON.stringify(value);
    return String(value);
  }

  const parsed = $derived(readSnapshot(snapshot));
  const sections = $derived<SnapshotSection[]>(parsed?.sections ?? []);
</script>

{#if sections.length}
  <details
    class="settings-snapshot mt-3 overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--paper-strong)]"
  >
    <summary
      class="flex cursor-pointer list-none items-center gap-2 px-3 py-2.5 text-xs font-semibold"
    >
      <SlidersHorizontal class="text-[var(--accent)]" size={14} />
      <span class="flex-1">Settings used</span>
      {#if parsed?.hash}<span class="muted font-mono font-normal"
          >{parsed.hash.slice(0, 8)}</span
        >{/if}
      <span class="snapshot-chevron muted"><ChevronDown size={15} /></span>
    </summary>
    <div
      class="grid gap-4 border-t border-[var(--line)] p-3 md:grid-cols-2 xl:grid-cols-3"
    >
      {#each sections as section (section.key)}
        <section>
          <h4 class="text-xs font-semibold">{section.label}</h4>
          <dl class="mt-2 space-y-1.5">
            {#each Object.entries(section.values) as [key, value] (key)}
              <div
                class="grid grid-cols-[minmax(0,1fr)_minmax(6rem,1fr)] gap-3 text-[.7rem]"
              >
                <dt class="muted">{humanize(key)}</dt>
                <dd
                  class="min-w-0 break-words text-right font-medium"
                  title={formatValue(value)}
                >
                  {formatValue(value)}
                </dd>
              </div>
            {/each}
          </dl>
        </section>
      {/each}
    </div>
  </details>
{/if}

<style>
  .settings-snapshot summary::-webkit-details-marker {
    display: none;
  }
  .snapshot-chevron {
    transition: transform 160ms ease;
  }
  .settings-snapshot[open] .snapshot-chevron {
    transform: rotate(180deg);
  }
  @media (prefers-reduced-motion: reduce) {
    .snapshot-chevron {
      transition: none;
    }
  }
</style>
