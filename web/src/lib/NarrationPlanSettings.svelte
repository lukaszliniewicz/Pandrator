<script lang="ts">
  import { onMount } from 'svelte';
  import { beforeNavigate, goto } from '$app/navigation';
  import { apiJson } from './api';
  import { sessionApi } from './domain-api';
  import type { SettingsPayload } from './api-models';
  import { errorMessage } from './errors';
  import { invalidationBus, invalidates } from './invalidation';
  import { notifySessionFlowChange } from './session-flow';
  import { modalDialog } from './modal-dialog';
  import NarrationPackingSettings from './NarrationPackingSettings.svelte';
  import ParameterLabel from './ParameterLabel.svelte';
  import { useProjectInterface } from './project-interface.svelte';
  const projectInterface = useProjectInterface();
  let {
    sessionId,
    busy = false,
    ttsOverride = {},
    embedded = false,
    onchanged
  }: {
    sessionId: string;
    busy?: boolean;
    ttsOverride?: Record<string, unknown>;
    embedded?: boolean;
    onchanged?: () => void | Promise<void>;
  } = $props();
  type Budget = {
    mode: 'model' | 'manual';
    target_chars: number;
    reason: string;
    policy_limit_chars: number;
    input_limit_chars?: number | null;
  };
  let settings = $state<SettingsPayload | null>(null);
  let budget = $state<Budget | null>(null);
  let modelDefault = $state<Budget | null>(null);
  let previewBudget = $state<Budget | null>(null);
  let previewLoading = $state(false);
  let previewError = $state('');
  let policy = $state<'model' | 'manual'>('model');
  let characters = $state<number | undefined>();
  let sentencePacking = $state<'auto' | 'sentences' | 'clauses'>('auto');
  let sentencePackingThreshold = $state<number | undefined>(300);
  let dialogueFlow = $state('auto');
  const initialDialogueFlow = $derived(
    String(settings?.effective.audiobook_dialogue_flow ?? 'auto')
  );
  let loading = $state(false),
    saving = $state(false),
    error = $state(''),
    notice = $state('');
  let serial = 0;
  let destination = $state<{ url: URL; replace: boolean } | null>(null);
  let allowNavigation = false;
  $effect(() => {
    if (!settings) return;
    const body = JSON.stringify({
      tts: ttsOverride,
      text: {
        audiobook_chunking: policy,
        ...(Number.isInteger(characters)
          ? { max_sentence_length: characters }
          : {})
      }
    });
    const controller = new AbortController();
    previewLoading = true;
    previewBudget = null;
    previewError = '';
    const timer = setTimeout(() => {
      void apiJson<{
        segmentation: Budget;
        model_default_segmentation: Budget;
      }>(
        `/sessions/${encodeURIComponent(sessionId)}/audiobook-segmentation-preview`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body,
          signal: controller.signal
        }
      )
        .then((result) => {
          if (controller.signal.aborted) return;
          previewBudget = result.segmentation;
          modelDefault = result.model_default_segmentation;
        })
        .catch((caught: unknown) => {
          if (!controller.signal.aborted) previewError = errorMessage(caught);
        })
        .finally(() => {
          if (!controller.signal.aborted) previewLoading = false;
        });
    }, 150);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  });
  const initialCharacters = $derived(
    budget?.mode === 'manual'
      ? Number(settings?.effective.max_sentence_length ?? budget.target_chars)
      : budget?.target_chars
  );
  const initialPacking = $derived(
    settings?.effective.audiobook_sentence_packing === 'sentences' ||
      settings?.effective.audiobook_sentence_packing === 'clauses'
      ? settings.effective.audiobook_sentence_packing
      : 'auto'
  );
  const initialPackingThreshold = $derived(
    Number(settings?.effective.audiobook_sentence_packing_threshold ?? 300)
  );
  const dirty = $derived(
    Boolean(
      budget &&
      (policy !== budget.mode ||
        (policy === 'manual' && characters !== initialCharacters) ||
        sentencePacking !== initialPacking ||
        sentencePackingThreshold !== initialPackingThreshold ||
        dialogueFlow !== initialDialogueFlow)
    )
  );
  const valid = $derived(
    (policy === 'model' ||
      (Number.isInteger(characters) &&
        Number(characters) >= 20 &&
        Number(characters) <= 8192)) &&
      Number.isInteger(sentencePackingThreshold) &&
      Number(sentencePackingThreshold) >= 1 &&
      Number(sentencePackingThreshold) <= 8192
  );
  function reset() {
    if (budget) policy = budget.mode;
    characters = initialCharacters;
    sentencePacking = initialPacking;
    sentencePackingThreshold = initialPackingThreshold;
    dialogueFlow = initialDialogueFlow;
  }
  beforeNavigate((navigation) => {
    if (
      allowNavigation ||
      (!dirty && !saving) ||
      navigation.willUnload ||
      navigation.to?.url.pathname === navigation.from?.url.pathname
    )
      return;
    navigation.cancel();
    if (!saving && navigation.to)
      destination = {
        url: navigation.to.url,
        replace: navigation.type === 'popstate'
      };
  });
  async function load() {
    const request = ++serial;
    loading = true;
    try {
      const [stored, setup] = await Promise.all([
        sessionApi.settings(sessionId, 'text'),
        apiJson<{ segmentation: Budget; model_default_segmentation: Budget }>(
          `/sessions/${encodeURIComponent(sessionId)}/audiobook-setup`
        )
      ]);
      if (request !== serial) return;
      settings = stored;
      budget = setup.segmentation;
      policy = budget.mode;
      modelDefault = setup.model_default_segmentation;
      characters = initialCharacters;
      sentencePacking = initialPacking;
      sentencePackingThreshold = initialPackingThreshold;
      dialogueFlow = initialDialogueFlow;
      error = '';
    } catch (caught) {
      if (request === serial) error = errorMessage(caught);
    } finally {
      if (request === serial) loading = false;
    }
  }
  export function validate() {
    if (dirty && !valid)
      throw new Error(
        'Enter a valid narration length and whole-sentence threshold before saving.'
      );
  }
  export async function persist() {
    validate();
    if (!dirty) return;
    if (!settings || !valid || busy || saving)
      throw new Error('Enter a valid narration length before saving.');
    saving = true;
    notice = '';
    error = '';
    try {
      await sessionApi.patchSettings(sessionId, 'text', settings.revision, {
        audiobook_chunking: policy,
        audiobook_sentence_packing: sentencePacking,
        audiobook_sentence_packing_threshold: sentencePackingThreshold,
        audiobook_dialogue_flow: dialogueFlow,
        ...(policy === 'manual' ? { max_sentence_length: characters } : {})
      });
      await load();
      notifySessionFlowChange(sessionId);
      await onchanged?.();
      notice =
        'Saved. Generation updates untouched narration blocks when their length changes. Rerun Segment narration for sentence or dialogue changes.';
    } catch (caught) {
      error = errorMessage(caught);
      throw caught;
    } finally {
      saving = false;
    }
  }
  async function save() {
    try {
      await persist();
    } catch {
      /* The field displays the save error. */
    }
  }
  onMount(() => {
    void load();
    const releaseGuard = projectInterface?.guard(async () => !dirty && !saving);
    const disconnect = invalidationBus.subscribe((batch) => {
      if (
        !dirty &&
        !saving &&
        (invalidates(batch, 'sessions', sessionId) ||
          invalidates(batch, 'workflow', sessionId))
      )
        void load();
    });
    return () => {
      serial++;
      disconnect();
      releaseGuard?.();
    };
  });
</script>

<svelte:window
  onbeforeunload={(event) => {
    if (dirty || saving) event.preventDefault();
  }}
/>

<fieldset
  class="narration-length"
  disabled={busy || loading || saving || !budget}
  aria-label="Narration length"
>
  <legend>Length of new narration blocks</legend>
  <div class="length-controls">
    <label
      ><ParameterLabel
        section="text"
        name="audiobook_chunking"
        label="Length policy"
        description="Automatic follows the selected speech model and its tuning. Custom limit keeps your chosen character budget when the model changes. Generation updates untouched narration blocks when their effective length changes; edited plans keep their boundaries."
      /><select class="input" bind:value={policy}
        ><option value="model">Automatic · model default</option><option
          value="manual">Custom limit</option
        ></select
      ></label
    >
    <label
      >{policy === 'model'
        ? 'Model default (characters)'
        : 'Maximum characters'}<input
        class="input"
        type="number"
        min="20"
        max="8192"
        step="1"
        required
        disabled={policy === 'model'}
        value={policy === 'model'
          ? previewLoading || previewError
            ? undefined
            : modelDefault?.target_chars
          : characters}
        oninput={(event) => (characters = event.currentTarget.valueAsNumber)}
      /></label
    >
    {#if dirty && !embedded}<button
        type="button"
        class="btn btn-secondary"
        disabled={!valid}
        onclick={save}>Save narration settings</button
      ><button type="button" class="btn" onclick={reset}>Reset</button>{/if}
  </div>
  <NarrationPackingSettings
    bind:policy={sentencePacking}
    bind:threshold={sentencePackingThreshold}
  />
  <label class="block text-sm">
    <ParameterLabel
      section="text"
      name="audiobook_dialogue_flow"
      label="Dialogue flow"
      description="On by default for new projects. Recognises clear, short dialogue exchanges using punctuation and language rules, without an LLM. Compatible turns can share a block and use sentence-length pauses. It does not identify characters or assign voices. Your explicit Off setting is preserved."
    />
    <select class="input mt-2 w-full" bind:value={dialogueFlow}>
      <option value="auto">Recognise short dialogue exchanges (default)</option>
      <option value="off">Off · keep paragraph pauses</option>
    </select>
  </label>
  <p>
    {#if previewLoading}Updating model length…{:else if previewBudget}{previewBudget.target_chars.toLocaleString()}
      characters per block · {policy === 'model'
        ? 'Selected model default.'
        : 'Current effective length.'}{:else}Loading model defaults…{/if} Clear dialogue
    turns can share a block when Dialogue flow is enabled. Chapter and scene breaks
    stay separate. Untouched narration is updated before generation when this length
    changes. Edited and annotated plans keep their boundaries.
  </p>
</fieldset>
{#if previewError}<p role="alert">
    Could not update the model length: {previewError}
  </p>{/if}
{#if error}<p role="alert" class="text-sm text-red-700">
    {error}
    <button type="button" class="underline" disabled={saving} onclick={load}
      >{dirty ? 'Discard changes and reload' : 'Reload length settings'}</button
    >
  </p>{/if}
{#if notice}<p role="status" class="muted text-xs">{notice}</p>{/if}
{#if destination}<dialog
    use:modalDialog={{ onclose: () => (destination = null) }}
    class="length-confirm"
    aria-labelledby="length-unsaved-title"
  >
    <h3 id="length-unsaved-title">Keep your narration changes?</h3>
    <p>The narration settings have unsaved changes.</p>
    <div class="confirm-actions">
      <button type="button" class="btn" onclick={() => (destination = null)}
        >Keep editing</button
      ><button
        type="button"
        class="btn btn-primary"
        onclick={async () => {
          const next = destination;
          reset();
          destination = null;
          if (next) {
            allowNavigation = true;
            try {
              await goto(next.url, { replaceState: next.replace });
            } finally {
              allowNavigation = false;
            }
          }
        }}>Discard and continue</button
      >
    </div>
  </dialog>{/if}

<style>
  .narration-length {
    padding: 0.8rem;
    border: 1px solid var(--line);
    border-radius: 0.7rem;
  }
  legend {
    padding: 0 0.3rem;
    font-size: 0.8rem;
    font-weight: 600;
  }
  .length-controls {
    display: flex;
    flex-wrap: wrap;
    align-items: end;
    gap: 0.75rem;
  }
  label {
    display: grid;
    gap: 0.3rem;
    flex: 1;
    min-width: 10rem;
    font-size: 0.72rem;
    font-weight: 600;
  }
  .input {
    padding: 0.5rem 0.65rem;
    border: 1px solid var(--line);
    border-radius: 0.45rem;
    background: var(--paper);
    color: var(--ink);
    font-size: 0.8rem;
    font-weight: 400;
  }
  input:disabled {
    opacity: 0.8;
  }
  p {
    margin-top: 0.55rem;
    font-size: 0.7rem;
    line-height: 1.5;
    color: var(--muted);
  }
  .length-confirm {
    width: min(26rem, calc(100vw - 2rem));
    margin: auto;
    padding: 1.25rem;
    border: 1px solid var(--line);
    border-radius: 1rem;
    background: var(--paper-strong);
    color: var(--ink);
  }
  .length-confirm::backdrop {
    background: rgb(0 0 0 / 40%);
  }
  .length-confirm h3 {
    font-weight: 600;
  }
  .confirm-actions {
    display: flex;
    justify-content: end;
    gap: 0.5rem;
    margin-top: 1rem;
  }
</style>
