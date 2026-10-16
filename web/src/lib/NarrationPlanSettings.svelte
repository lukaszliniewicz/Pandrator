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
  let {
    sessionId,
    busy = false,
    onchanged
  }: {
    sessionId: string;
    busy?: boolean;
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
  let policy = $state<'model' | 'manual'>('model');
  let characters = $state<number | undefined>();
  let loading = $state(false),
    saving = $state(false),
    error = $state(''),
    notice = $state('');
  let serial = 0;
  let destination = $state<{ url: URL; replace: boolean } | null>(null);
  let allowNavigation = false;
  const initialCharacters = $derived(
    budget?.mode === 'manual'
      ? Number(settings?.effective.max_sentence_length ?? budget.target_chars)
      : budget?.target_chars
  );
  const dirty = $derived(
    Boolean(
      budget &&
      (policy !== budget.mode ||
        (policy === 'manual' && characters !== initialCharacters))
    )
  );
  const valid = $derived(
    policy === 'model' ||
      (Number.isInteger(characters) &&
        Number(characters) >= 20 &&
        Number(characters) <= 8192)
  );
  function reset() {
    if (budget) policy = budget.mode;
    characters = initialCharacters;
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
      error = '';
    } catch (caught) {
      if (request === serial) error = errorMessage(caught);
    } finally {
      if (request === serial) loading = false;
    }
  }
  async function save() {
    if (!settings || !dirty || !valid || busy || saving) return;
    saving = true;
    notice = '';
    error = '';
    try {
      await sessionApi.patchSettings(sessionId, 'text', settings.revision, {
        audiobook_chunking: policy,
        ...(policy === 'manual' ? { max_sentence_length: characters } : {})
      });
      await load();
      notifySessionFlowChange(sessionId);
      await onchanged?.();
      notice =
        'Saved. Run Segment narration again, then prepare a new plan, to use this length.';
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      saving = false;
    }
  }
  onMount(() => {
    void load();
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
      >Length policy<select class="input" bind:value={policy}
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
        value={policy === 'model' ? modelDefault?.target_chars : characters}
        oninput={(event) => (characters = event.currentTarget.valueAsNumber)}
      /></label
    >
    {#if dirty}<button
        type="button"
        class="btn btn-secondary"
        disabled={!valid}
        onclick={save}>Save length</button
      ><button type="button" class="btn" onclick={reset}>Reset</button>{/if}
  </div>
  <p>
    {#if budget}{budget.target_chars.toLocaleString()} characters per block · {policy ===
      'model'
        ? 'Complete sentences, with model headroom.'
        : 'Current effective length.'}{:else}Loading model defaults…{/if} Clear dialogue
    turns can share a block when Dialogue flow is enabled. Chapter and scene breaks
    stay separate. Existing plans keep their boundaries.
  </p>
</fieldset>
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
    <h3 id="length-unsaved-title">Keep your length changes?</h3>
    <p>The narration length has unsaved changes.</p>
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
