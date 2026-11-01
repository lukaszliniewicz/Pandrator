<script lang="ts">
  import { modalDialog } from './modal-dialog';

  let {
    onsave,
    ondiscard,
    oncancel,
    busy = false,
    error = '',
    saveLabel = 'Save',
    description = 'Your changes have not been saved. A failed save keeps this editor open.'
  }: {
    onsave: () => void;
    ondiscard: () => void;
    oncancel: () => void;
    busy?: boolean;
    error?: string;
    saveLabel?: string;
    description?: string;
  } = $props();
  const id = $props.id();
</script>

<dialog
  use:modalDialog={{
    onclose: () => {
      if (!busy) oncancel();
    }
  }}
  role="alertdialog"
  aria-labelledby={`${id}-title`}
  aria-describedby={`${id}-description`}
  class="draft-dialog compact-confirmation"
>
  <h3 id={`${id}-title`} class="font-semibold">Save your changes?</h3>
  <p id={`${id}-description`} class="muted mt-2 text-sm">{description}</p>
  {#if error}<p role="alert" class="mt-3 text-sm text-red-600">{error}</p>{/if}
  <div class="mt-4 flex flex-wrap justify-end gap-2">
    <button class="btn btn-secondary" disabled={busy} onclick={oncancel}
      >Keep editing</button
    >
    <button class="btn btn-secondary" disabled={busy} onclick={ondiscard}
      >Discard changes</button
    >
    <button class="btn btn-primary" disabled={busy} onclick={onsave}
      >{busy ? 'Saving…' : saveLabel}</button
    >
  </div>
</dialog>

<style>
  .draft-dialog {
    width: min(28rem, calc(100vw - 2rem));
    max-height: calc(100dvh - 2rem);
    margin: auto;
    padding: 1.25rem;
    overflow: auto;
    border: 1px solid var(--line);
    border-radius: 1rem;
    background: var(--paper);
    color: var(--ink);
  }
  .draft-dialog::backdrop {
    background: rgb(0 0 0 / 45%);
  }
</style>
