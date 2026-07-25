<script lang="ts">
  export type CredentialBackendProfile = {
    id: 'database' | 'keyring' | 'environment' | 'file';
    label: string;
    available: boolean;
    default: boolean;
    requires_secret: boolean;
    requires_reference: boolean;
    description: string;
    guidance: string;
  };

  let {
    backends,
    backend = $bindable('database'),
    reference = $bindable(''),
    secret = $bindable(''),
    deletePrevious = $bindable(false),
    configured = false,
    currentSource = 'none',
    existingBackend = 'database',
    suggestedEnvironment = '',
    secretLabel = 'API key',
    multiline = false,
    onsecretblur = () => undefined
  }: {
    backends: CredentialBackendProfile[];
    backend?: string;
    reference?: string;
    secret?: string;
    deletePrevious?: boolean;
    configured?: boolean;
    currentSource?: string;
    existingBackend?: string;
    suggestedEnvironment?: string;
    secretLabel?: string;
    multiline?: boolean;
    onsecretblur?: () => void;
  } = $props();

  const selected = $derived(backends.find((item) => item.id === backend));
  const moving = $derived(configured && backend !== existingBackend);

  function selectBackend(value: string) {
    backend = value;
    secret = '';
    deletePrevious = false;
    if (value === 'environment' && !reference.trim()) {
      reference = suggestedEnvironment;
    } else if (value !== 'environment' && value !== 'file') {
      reference = '';
    }
  }
</script>

<fieldset class="rounded-xl border border-[var(--line)] p-4">
  <legend class="px-1 text-sm font-semibold">Credential storage</legend>
  <label class="block text-sm font-semibold">Storage method
    <select value={backend} onchange={(event) => selectBackend(event.currentTarget.value)} class="field">
      {#each backends as item}
        <option value={item.id} disabled={!item.available}>{item.label}{item.available ? '' : ' (unavailable)'}</option>
      {/each}
    </select>
  </label>

  {#if selected}
    <p class="muted mt-2 text-xs leading-relaxed">{selected.description}</p>
    <p class="muted mt-1 text-xs leading-relaxed">{selected.guidance}</p>
  {/if}

  {#if backend === 'database' || backend === 'keyring'}
    <label class="mt-4 block text-sm font-semibold">{secretLabel}
      {#if multiline}
        <textarea
          bind:value={secret}
          oninput={() => deletePrevious = false}
          onblur={onsecretblur}
          rows="7"
          autocomplete="off"
          spellcheck="false"
          placeholder={configured && backend === existingBackend ? 'Leave blank to keep the current credential' : `Enter ${secretLabel.toLowerCase()}`}
          class="field font-mono text-xs"
        ></textarea>
      {:else}
        <input
          bind:value={secret}
          oninput={() => deletePrevious = false}
          type="password"
          autocomplete="new-password"
          placeholder={configured && backend === existingBackend ? 'Leave blank to keep the current credential' : `Enter ${secretLabel.toLowerCase()}`}
          class="field"
        />
      {/if}
    </label>
  {:else if backend === 'environment'}
    <label class="mt-4 block text-sm font-semibold">Environment variable name
      <input bind:value={reference} autocomplete="off" spellcheck="false" placeholder={suggestedEnvironment || 'OPENAI_API_KEY'} class="field font-mono"/>
      <small class="muted mt-1 block font-normal">The variable must already be visible to the running Pandrator process. Persistent changes normally require an application restart.</small>
    </label>
  {:else if backend === 'file'}
    <label class="mt-4 block text-sm font-semibold">Absolute secret-file path
      <input bind:value={reference} autocomplete="off" spellcheck="false" placeholder="/run/secrets/provider-key" class="field font-mono"/>
      <small class="muted mt-1 block font-normal">The file must contain only the UTF-8 secret value. On macOS/Linux it must have owner-only permissions.</small>
    </label>
  {/if}

  {#if configured}
    <p class="muted mt-3 text-xs">Current resolved source: {currentSource}.</p>
  {/if}
  {#if moving && ['database', 'keyring'].includes(existingBackend)}
    <label class="mt-3 flex items-start gap-2 text-sm">
      <input type="checkbox" bind:checked={deletePrevious} class="mt-0.5 accent-[var(--accent)]"/>
      <span>After the new backend is verified, remove the old app-managed value.<small class="muted mt-1 block">Shared LLM/TTS credentials are retained when another connection may still use them.</small></span>
    </label>
  {/if}
</fieldset>

<style>
  .field{margin-top:.4rem;width:100%;min-width:0;border:1px solid var(--line);border-radius:.72rem;background:var(--paper);padding:.65rem .72rem;font-weight:400;color:var(--ink)}
</style>
