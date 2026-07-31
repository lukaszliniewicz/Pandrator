<script lang="ts">
  import { errorMessage } from './errors';
  import { CheckCircle2, KeyRound, Trash2 } from '@lucide/svelte';
  import { credentialApi } from './admin-api';
  import CredentialStorageFields, {
    type CredentialBackendProfile
  } from './CredentialStorageFields.svelte';

  type CredentialProfile = {
    id: string;
    label: string;
    description: string;
    environment_variable: string;
    credential_configured: boolean;
    credential_source: string;
    credential_backend: string;
    credential_reference: string;
    previous_credential_retained?: boolean;
  };

  let items = $state<CredentialProfile[]>([]);
  let credentialBackends = $state<CredentialBackendProfile[]>([]);
  let values = $state<Record<string, string>>({});
  let selectedBackends = $state<Record<string, string>>({});
  let references = $state<Record<string, string>>({});
  let deletePrevious = $state<Record<string, boolean>>({});
  let busy = $state('');
  let error = $state('');
  let notice = $state('');

  async function load() {
    try {
      const [credentialPayload, backendPayload] = await Promise.all([
        credentialApi.list<CredentialProfile>(),
        credentialApi.backends<CredentialBackendProfile>()
      ]);
      items = credentialPayload.items;
      credentialBackends = backendPayload.items;
      for (const item of items) {
        selectedBackends[item.id] ??= item.credential_backend || 'database';
        references[item.id] ??= item.credential_reference || '';
        deletePrevious[item.id] ??= false;
      }
      error = '';
    } catch (caught) {
      error = errorMessage(caught);
    }
  }

  async function save(item: CredentialProfile) {
    const apiKey = String(values[item.id] ?? '').trim();
    const backend = selectedBackends[item.id] ?? 'database';
    if (
      ['database', 'keyring'].includes(backend) &&
      !apiKey &&
      !(item.credential_configured && backend === item.credential_backend)
    ) {
      error = `Enter a ${item.label} API key.`;
      return;
    }
    busy = item.id;
    error = '';
    notice = '';
    try {
      const updated = await credentialApi.update<CredentialProfile>(item.id, {
        credential_backend: backend as
          'database' | 'environment' | 'keyring' | 'file',
        credential_reference: String(references[item.id] ?? '').trim() || null,
        delete_previous_credential: Boolean(deletePrevious[item.id]),
        ...(apiKey ? { api_key: apiKey } : {})
      });
      values = { ...values, [item.id]: '' };
      deletePrevious = { ...deletePrevious, [item.id]: false };
      notice =
        updated.previous_credential_retained &&
        backend !== item.credential_backend
          ? `${item.label} updated; the previous credential was retained.`
          : `${item.label} credential saved.`;
      await load();
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      busy = '';
    }
  }

  async function remove(item: CredentialProfile) {
    busy = item.id;
    error = '';
    notice = '';
    try {
      await credentialApi.update<CredentialProfile>(item.id, { clear: true });
      values = { ...values, [item.id]: '' };
      selectedBackends = { ...selectedBackends, [item.id]: 'database' };
      references = { ...references, [item.id]: '' };
      deletePrevious = { ...deletePrevious, [item.id]: false };
      notice = `${item.label} credential reference removed.`;
      await load();
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      busy = '';
    }
  }

  load();
</script>

<section>
  <header>
    <div class="eyebrow">Other API keys</div>
    <h2 class="mt-1 text-2xl font-semibold">Supporting services</h2>
    <p class="muted mt-2 max-w-2xl text-sm">
      Keys used outside the LLM and speech provider catalogues are managed here
      with the same write-only behavior. Direct database entry remains the
      default and requires no operating-system setup.
    </p>
  </header>
  {#if error}<p
      role="alert"
      class="mt-4 rounded-xl border border-red-400/40 bg-red-500/10 p-3 text-sm text-red-600"
    >
      {error}
    </p>{/if}
  {#if notice}<p
      role="status"
      class="mt-4 flex items-center gap-2 rounded-xl border border-[var(--line)] bg-[var(--accent-soft)] p-3 text-sm"
    >
      <CheckCircle2 size={16} />{notice}
    </p>{/if}
  <div class="mt-5 grid gap-4 lg:grid-cols-2">
    {#each items as item}
      <article class="rounded-2xl border border-[var(--line)] p-5">
        <div class="flex items-start gap-3">
          <div
            class="grid size-10 shrink-0 place-items-center rounded-xl bg-[var(--accent-soft)] text-[var(--accent)]"
          >
            <KeyRound size={18} />
          </div>
          <div>
            <div class="flex flex-wrap items-center gap-2">
              <h3 class="font-semibold">{item.label}</h3>
              <span
                class="rounded-full border border-[var(--line)] px-2 py-0.5 text-[.62rem] font-bold uppercase"
                >{item.credential_configured
                  ? `Key: ${item.credential_source}`
                  : 'No key'}</span
              >
            </div>
            <p class="muted mt-1 text-xs">{item.description}</p>
          </div>
        </div>
        <div class="mt-5">
          <CredentialStorageFields
            backends={credentialBackends}
            bind:backend={selectedBackends[item.id]}
            bind:reference={references[item.id]}
            bind:secret={values[item.id]}
            bind:deletePrevious={deletePrevious[item.id]}
            configured={item.credential_configured}
            currentSource={item.credential_source}
            existingBackend={item.credential_backend}
            suggestedEnvironment={item.environment_variable}
          />
        </div>
        <div class="mt-4 flex flex-wrap gap-2">
          <button
            onclick={() => save(item)}
            disabled={busy === item.id}
            class="btn btn-primary"
            >{selectedBackends[item.id] !== item.credential_backend
              ? 'Verify move'
              : item.credential_configured
                ? 'Update credential'
                : 'Save credential'}</button
          >{#if item.credential_configured}<button
              onclick={() => remove(item)}
              disabled={busy === item.id}
              class="btn btn-secondary text-red-500"
              ><Trash2 size={14} /> Remove credential</button
            >{/if}
        </div>
      </article>
    {/each}
  </div>
</section>
