<script lang="ts">
  import { Check, Eye, LoaderCircle, X } from '@lucide/svelte';
  import { onMount, tick } from 'svelte';
  import { apiJson } from './api';
  import { sessionApi } from './domain-api';
  import { errorMessage } from './errors';
  import type { GenerationControls } from './generation-controls';
  import type { SpeechSelection } from './speech-annotations';
  import { notifySessionFlowChange } from './session-flow';
  import { voiceLibraryApi } from './voice-library-api';

  type Preview = {
    preview_revision: string;
    locked: boolean;
    preview: {
      parts?: Array<{
        voice?: string;
        voice_source?: string;
        instructions?: string;
        text?: string;
        report?: Array<{ status: string; message: string }>;
      }>;
      report?: Array<{ status: string; message: string }>;
    };
  };
  let {
    sessionId,
    revisionId,
    selection,
    onclose,
    onapplied
  }: {
    sessionId: string;
    revisionId: string;
    selection: SpeechSelection;
    onclose: () => void;
    onapplied: () => void | Promise<void>;
  } = $props();
  let panel: HTMLDivElement;
  let controls = $state<GenerationControls | null>(null);
  let pending = $state(false);
  let error = $state('');
  let speaker = $state('unchanged');
  let voice = $state('__keep__');
  let instruction = $state(''),
    emotion = $state(''),
    pace = $state(''),
    cadence = $state(''),
    emphasis = $state('');
  let clearDelivery = $state(false),
    unlock = $state(false);
  let castingEnabled = $state(false),
    performanceEnabled = $state(false);
  let enableCasting = $state(false),
    enablePerformance = $state(false);
  let preview = $state<Preview | null>(null);
  let previewBody = $state<Record<string, unknown> | null>(null);
  let libraryVoices = $state<Array<{ value: string; label: string }>>([]);
  const spoken = $derived(selection.item.optimized_text ?? selection.item.text);
  const quote = $derived(
    Array.from(spoken).slice(selection.start, selection.end).join('')
  );
  const voices = $derived.by(() => {
    if (!controls) return [];
    const bindings = [
      controls.cast.narrator,
      ...Object.values(controls.cast.characters),
      ...Object.values(controls.cast.categories)
    ];
    const options = new Map(
      libraryVoices.map((item) => [item.value, item.label])
    );
    for (const binding of bindings) {
      if (binding?.voice && !options.has(binding.voice))
        options.set(binding.voice, binding.voice);
    }
    return [...options].map(([value, label]) => ({ value, label }));
  });
  const hasChanges = $derived(
    speaker !== 'unchanged' ||
      voice !== '__keep__' ||
      Boolean(
        instruction.trim() ||
        emotion.trim() ||
        pace ||
        cadence.trim() ||
        emphasis.trim() ||
        clearDelivery
      )
  );
  const reports = $derived.by(() => {
    const items =
      preview?.preview.report ??
      preview?.preview.parts?.flatMap((part) => part.report ?? []) ??
      [];
    return [
      ...new Map(
        items.map((item) => [`${item.status}:${item.message}`, item])
      ).values()
    ];
  });
  const base = $derived(
    `/sessions/${encodeURIComponent(sessionId)}/speech-plan`
  );
  function body() {
    return {
      revision_id: revisionId,
      segment_id: selection.item.id,
      expected_segment_revision: selection.item.revision,
      start: selection.start,
      end: selection.end,
      speaker:
        speaker === 'unchanged' || speaker === 'narrator'
          ? speaker
          : 'character',
      ...(speaker !== 'unchanged' && speaker !== 'narrator'
        ? { character_id: speaker }
        : {}),
      ...(voice === '__keep__' ? {} : { voice: voice || null }),
      delivery: clearDelivery
        ? {
            instruction: null,
            emotion: null,
            pace: null,
            cadence: null,
            emphasis: null
          }
        : {
            ...(instruction.trim() ? { instruction: instruction.trim() } : {}),
            ...(emotion.trim() ? { emotion: emotion.trim() } : {}),
            ...(pace ? { pace } : {}),
            ...(cadence.trim() ? { cadence: cadence.trim() } : {}),
            ...(emphasis.trim() ? { emphasis: emphasis.trim() } : {})
          },
      unlock_locked: unlock,
      enable_casting: enableCasting,
      enable_performance: enablePerformance
    };
  }
  async function inspect() {
    if (!hasChanges) return;
    pending = true;
    error = '';
    try {
      const candidate = body();
      preview = await apiJson<Preview>(`${base}/selection-preview`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(candidate)
      });
      previewBody = candidate;
      await tick();
      panel.focus();
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      pending = false;
    }
  }
  async function apply() {
    if (!preview || !previewBody) return;
    pending = true;
    error = '';
    try {
      await apiJson(`${base}/selection`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          ...previewBody,
          unlock_locked: unlock,
          expected_preview_revision: preview.preview_revision
        })
      });
      notifySessionFlowChange(sessionId);
      await onapplied();
      selection.anchor.focus();
      onclose();
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      pending = false;
    }
  }
  function close() {
    if (!pending) {
      selection.anchor.focus();
      onclose();
    }
  }
  async function returnToEdit() {
    preview = null;
    previewBody = null;
    await tick();
    panel.querySelector<HTMLSelectElement>('select')?.focus();
  }
  onMount(() => {
    let alive = true;
    Promise.all([
      apiJson<GenerationControls>(
        `/sessions/${encodeURIComponent(sessionId)}/generation-controls`
      ),
      sessionApi.settings(sessionId, 'tts')
    ])
      .then(async ([dictionary, settings]) => {
        if (!alive) return;
        controls = dictionary;
        castingEnabled = Boolean(settings.effective.casting_enabled);
        performanceEnabled = Boolean(settings.effective.performance_enabled);
        const service = String(settings.effective.service ?? '');
        const model = String(
          settings.effective.model ?? settings.effective.xtts_model ?? ''
        );
        const catalog = await voiceLibraryApi.query({
          service_id: service,
          model,
          limit: 200
        });
        if (!alive) return;
        libraryVoices = catalog.items.flatMap((item) => {
          const compatible = item.compatibility.find(
            (candidate) =>
              candidate.service_id === service &&
              candidate.model === model &&
              candidate.ready
          );
          const value =
            compatible?.voice ||
            (compatible && item.reference.kind === 'provider'
              ? item.reference.voice
              : '');
          return value ? [{ value, label: item.name }] : [];
        });
      })
      .catch((caught) => {
        if (alive) error = errorMessage(caught);
      });
    return () => {
      alive = false;
    };
  });
  $effect(() => {
    if (!panel) return;
    panel.showPopover();
    panel.querySelector<HTMLSelectElement>('select')?.focus();
    const position = () => {
      const size = panel.getBoundingClientRect(),
        rect = selection.rect;
      panel.style.left = `${Math.max(8, Math.min(rect.left, innerWidth - size.width - 8))}px`;
      panel.style.top = `${rect.bottom + size.height + 12 < innerHeight ? rect.bottom + 6 : Math.max(8, rect.top - size.height - 6)}px`;
    };
    position();
    const observer = new ResizeObserver(position);
    observer.observe(panel);
    return () => observer.disconnect();
  });
</script>

<div
  bind:this={panel}
  popover="manual"
  role="dialog"
  tabindex="-1"
  aria-label="Edit selected speech"
  class="selection-editor font-sans"
  onkeydown={(event) => {
    event.stopPropagation();
    if (event.key === 'Escape') {
      event.preventDefault();
      close();
    }
  }}
>
  <header>
    <h3>Edit selected speech</h3>
    <button
      class="close"
      aria-label="Close selection editor"
      disabled={pending}
      onclick={close}><X size={17} /></button
    >
  </header>
  <blockquote>{quote}</blockquote>
  <p class="hint">
    The spoken words stay unchanged. Edits affect future audio.
  </p>
  {#if error}<p class="error" role="alert">{error}</p>{/if}
  {#if !preview}
    <fieldset disabled={pending}>
      <label
        >Speaker<select bind:value={speaker}
          ><option value="unchanged">Keep current speakers</option><option
            value="narrator">Narrator</option
          >{#each controls?.characters ?? [] as character}<option
              value={character.id}>{character.display_name}</option
            >{/each}</select
        ></label
      >
      <label
        >Voice<select bind:value={voice}
          ><option value="__keep__">Keep current voice choices</option><option
            value="">Use the speaker’s cast</option
          >{#each voices as option}<option value={option.value}
              >{option.label}</option
            >{/each}</select
        ></label
      >
      {#if !voices.length}<p class="hint">
          Publish a reference or assign a voice in the casting card to choose it
          here.
        </p>{/if}
      <label
        >Delivery instruction<textarea
          bind:value={instruction}
          disabled={clearDelivery}
          rows="2"
          maxlength="1200"
          placeholder="For example: quietly, with dry amusement"
        ></textarea></label
      >
      <div class="pair">
        <label
          >Emotion<input
            bind:value={emotion}
            disabled={clearDelivery}
            maxlength="80"
            placeholder="Leave unchanged"
          /></label
        ><label
          >Pace<select bind:value={pace} disabled={clearDelivery}
            ><option value="">Leave unchanged</option><option value="slower"
              >Slow</option
            ><option value="natural">Natural</option><option value="brisk"
              >Fast</option
            ></select
          ></label
        >
      </div>
      <details>
        <summary>More delivery controls</summary>
        <div class="pair">
          <label
            >Cadence<select bind:value={cadence} disabled={clearDelivery}
              ><option value="">Leave unchanged</option><option
                value="continuing">Continuing</option
              ><option value="concluding">Concluding</option><option
                value="questioning">Questioning</option
              ><option value="contrast">Contrast</option></select
            ></label
          ><label
            >Emphasis<select bind:value={emphasis} disabled={clearDelivery}
              ><option value="">Leave unchanged</option><option value="light"
                >Light</option
              ><option value="moderate">Moderate</option><option value="strong"
                >Strong</option
              ></select
            ></label
          >
        </div>
      </details>
      <label class="check"
        ><input type="checkbox" bind:checked={clearDelivery} /> Clear delivery directions
        in this phrase</label
      >
      {#if !castingEnabled}<label class="check"
          ><input type="checkbox" bind:checked={enableCasting} /> Enable character
          casting for future audio</label
        >{/if}
      {#if !performanceEnabled}<label class="check"
          ><input type="checkbox" bind:checked={enablePerformance} /> Enable delivery
          instructions for future audio</label
        >{/if}
    </fieldset>
    <button
      class="primary"
      disabled={pending || !hasChanges || !controls}
      onclick={inspect}
      >{#if pending}<LoaderCircle size={15} class="animate-spin" />{:else}<Eye
          size={15}
        />{/if} Preview change</button
    >
  {:else}
    <h4>Review before applying</h4>
    <p class="hint">Voice sequence for the whole block after this edit:</p>
    {#each preview.preview.parts ?? [] as part}<p class="part">
        <strong
          >{voices.find((option) => option.value === part.voice)?.label ||
            part.voice ||
            'Renderer default'}</strong
        >{#if part.voice_source === 'segment'}<span>Block voice override</span
          >{/if}{#if part.text}<span>{part.text}</span
          >{/if}{#if part.instructions}<span>{part.instructions}</span>{/if}
      </p>{/each}
    {#each reports as report}<p
        class="hint"
        class:warning={['unsupported', 'ignored', 'dropped'].includes(
          report.status
        )}
      >
        {report.message}
      </p>{/each}
    {#if !castingEnabled && !enableCasting}<p class="hint warning">
        Casting is off. Character assignments are saved but will not choose
        voices until casting is enabled.
      </p>{/if}
    {#if !performanceEnabled && !enablePerformance}<p class="hint warning">
        Delivery is off. Directions are saved but will not be sent to the
        renderer.
      </p>{/if}
    {#if preview.locked}<label class="check"
        ><input type="checkbox" bind:checked={unlock} disabled={pending} /> Unlock
        and replace the protected phrase settings</label
      >{/if}
    <p class="hint">
      The remaining speech stays unchanged. Previous settings and generated
      audio remain in history.
    </p>
    <div class="actions">
      <button disabled={pending} onclick={returnToEdit}>Back to edit</button
      ><button
        class="primary"
        disabled={pending || (preview.locked && !unlock)}
        onclick={apply}
        >{#if pending}<LoaderCircle
            size={15}
            class="animate-spin"
          />{:else}<Check size={15} />{/if} Apply reviewed change</button
      >
    </div>
  {/if}
</div>

<style>
  .selection-editor {
    position: fixed;
    margin: 0;
    width: min(420px, calc(100vw - 16px));
    max-height: calc(100dvh - 24px);
    overflow-y: auto;
    padding: 1rem;
    background: var(--paper-strong);
    color: var(--ink);
    border: 1px solid var(--line);
    border-radius: 0.9rem;
    box-shadow: var(--shadow);
    font-size: 0.8rem;
  }
  header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 0.6rem;
  }
  h3,
  h4 {
    font-weight: 700;
  }
  h4 {
    margin-top: 0.7rem;
  }
  .close {
    display: grid;
    place-items: center;
    width: 36px;
    height: 36px;
    border: 1px solid var(--line);
    border-radius: 0.4rem;
  }
  blockquote {
    margin: 0.5rem 0;
    padding: 0.5rem 0.7rem;
    border-left: 3px solid var(--accent);
    background: var(--accent-soft);
    line-height: 1.5;
    max-height: 7rem;
    overflow-y: auto;
    white-space: pre-wrap;
  }
  .hint {
    color: var(--muted);
    font-size: 0.72rem;
    line-height: 1.5;
    margin: 0.45rem 0;
  }
  .warning {
    color: var(--warning, #a35f15);
  }
  .error {
    color: var(--danger, #b42318);
    margin: 0.5rem 0;
  }
  fieldset {
    display: grid;
    gap: 0.65rem;
    margin: 0.75rem 0;
  }
  label {
    display: grid;
    gap: 0.3rem;
    font-weight: 600;
  }
  label.check {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    font-weight: 400;
    line-height: 1.5;
  }
  input:not([type='checkbox']),
  select,
  textarea {
    width: 100%;
    min-width: 0;
    padding: 0.55rem 0.6rem;
    border: 1px solid var(--line);
    border-radius: 0.45rem;
    background: var(--paper);
    font-weight: 400;
  }
  .pair {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 0.7rem;
  }
  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: 0.6rem;
    margin-top: 0.7rem;
  }
  .actions button,
  button.primary {
    display: inline-flex;
    justify-content: center;
    align-items: center;
    gap: 0.4rem;
    border: 1px solid var(--line);
    padding: 0.6rem 0.75rem;
    border-radius: 0.5rem;
    min-height: 40px;
    font-weight: 600;
  }
  button.primary {
    color: white;
    background: var(--accent);
  }
  button:disabled {
    opacity: 0.5;
  }
  button:focus-visible,
  input:focus-visible,
  select:focus-visible,
  textarea:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }
  .part {
    display: grid;
    gap: 0.25rem;
    padding: 0.5rem 0;
    border-bottom: 1px solid var(--line);
    overflow-wrap: anywhere;
  }
  @media (pointer: coarse) {
    button,
    select {
      min-height: 44px;
    }
  }
</style>
