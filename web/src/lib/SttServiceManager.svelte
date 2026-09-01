<script lang="ts">
  import { errorMessage } from './errors';
  import {
    CheckCircle2,
    ExternalLink,
    Settings2,
    Trash2,
    X
  } from '@lucide/svelte';
  import { credentialApi, settingApi, speechRecognitionApi } from './admin-api';
  import type {
    SttCatalogue,
    SttService,
    SttSettingsValue
  } from './api-models';
  import CredentialStorageFields, {
    type CredentialBackendProfile
  } from './CredentialStorageFields.svelte';
  import { modalFocus } from './modal-focus';

  type CredentialBackend = 'database' | 'environment' | 'keyring' | 'file';

  let payload = $state<SttCatalogue>({
    services: [],
    profiles: [],
    value: {},
    revision: 0
  });
  let credentialBackends = $state<CredentialBackendProfile[]>([]);
  let editing = $state<SttService | null>(null);
  let editingApiKey = $state('');
  let editingCredentialBackend = $state<CredentialBackend>('database');
  let editingExistingCredentialBackend = $state<CredentialBackend>('database');
  let editingCredentialReference = $state('');
  let deletePreviousCredential = $state(false);
  let removeCredential = $state(false);
  let busy = $state('');
  let error = $state('');

  const serviceId = (value: unknown) =>
    String(value ?? '')
      .trim()
      .toLowerCase()
      .replaceAll('-', '_');

  async function load() {
    const [catalogue, backends] = await Promise.all([
      speechRecognitionApi.catalogue(),
      credentialApi.backends<CredentialBackendProfile>()
    ]);
    payload = catalogue;
    credentialBackends = backends.items;
  }

  async function persist(value: SttSettingsValue) {
    await settingApi.put<SttSettingsValue>(
      'services.stt',
      payload.revision,
      value
    );
    await load();
  }

  function recordFrom(
    candidate: SttService,
    existing: Partial<SttService> = {}
  ): SttService {
    return {
      ...candidate,
      ...existing,
      id: candidate.id,
      name: candidate.name,
      adapter: candidate.adapter,
      api_base: existing.api_base ?? candidate.api_base,
      transcription_path: candidate.transcription_path,
      model: existing.model ?? candidate.model,
      models: candidate.models,
      api_key_env: candidate.api_key_env,
      settings: { ...(candidate.settings ?? {}), ...(existing.settings ?? {}) },
      secret_ref: existing.secret_ref ?? candidate.secret_ref
    };
  }

  async function useProfile(profile: SttService) {
    busy = profile.id;
    error = '';
    try {
      const existing = (payload.value.provider_configs ?? []).find(
        (item) => serviceId(item.id) === serviceId(profile.id)
      );
      await persist({
        ...payload.value,
        provider_configs: [
          ...(payload.value.provider_configs ?? []).filter(
            (item) => serviceId(item.id) !== serviceId(profile.id)
          ),
          recordFrom(profile, existing)
        ]
      });
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      busy = '';
    }
  }

  function openSettings(service: SttService) {
    editing = { ...service, settings: { ...(service.settings ?? {}) } };
    editingApiKey = '';
    editingCredentialBackend =
      service.credential_backend === 'environment' ||
      service.credential_backend === 'keyring' ||
      service.credential_backend === 'file'
        ? service.credential_backend
        : 'database';
    editingExistingCredentialBackend = editingCredentialBackend;
    editingCredentialReference = String(service.credential_reference ?? '');
    deletePreviousCredential = false;
    removeCredential = false;
  }

  function setEditingSetting(key: string, value: unknown) {
    if (!editing) return;
    editing = {
      ...editing,
      settings: { ...(editing.settings ?? {}), [key]: value }
    };
  }

  async function saveEditing() {
    if (!editing) return;
    busy = editing.id;
    error = '';
    try {
      const baseRecord = { ...editing };
      delete baseRecord.credential_backend;
      delete baseRecord.credential_reference;
      delete baseRecord.credential_configured;
      delete baseRecord.credential_source;
      const credential = removeCredential
        ? { clear_api_key: true }
        : {
            credential_backend: editingCredentialBackend,
            credential_reference: editingCredentialReference.trim() || null,
            delete_previous_credential: deletePreviousCredential,
            ...(editingApiKey.trim() ? { api_key: editingApiKey.trim() } : {})
          };
      await persist({
        ...payload.value,
        provider_configs: [
          ...(payload.value.provider_configs ?? []).filter(
            (item) => serviceId(item.id) !== serviceId(editing?.id)
          ),
          { ...baseRecord, ...credential }
        ]
      });
      editing = null;
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      busy = '';
    }
  }

  async function removeService(service: SttService) {
    busy = service.id;
    error = '';
    try {
      await persist({
        ...payload.value,
        provider_configs: (payload.value.provider_configs ?? []).filter(
          (item) => serviceId(item.id) !== serviceId(service.id)
        )
      });
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      busy = '';
    }
  }

  const timestampLabel = (service: SttService) =>
    service.word_timestamps === true
      ? 'Word timestamps'
      : service.word_timestamps === 'conditional' ||
          service.word_timestamps === 'unverified'
        ? 'Word timing checked at runtime'
        : 'No word timestamps';

  $effect(() => {
    load().catch((caught) => (error = errorMessage(caught)));
  });
</script>

<section>
  <div class="flex flex-wrap items-start justify-between gap-4">
    <div>
      <div class="eyebrow">Cloud transcription</div>
      <h2 class="mt-1 text-2xl font-semibold">Recognition connections</h2>
      <p class="muted mt-2 max-w-3xl text-sm">
        Configure timed speech-to-text services for dubbing and subtitle
        workflows. Pandrator requires genuine word timings; it never invents
        them when a provider omits them.
      </p>
    </div>
  </div>

  {#if error}<div class="error-banner mt-5">{error}</div>{/if}

  {#if payload.services.length}
    <div class="mt-6 grid gap-4 lg:grid-cols-2">
      {#each payload.services as service}
        <article class="surface p-5">
          <div class="flex items-start justify-between gap-4">
            <div class="min-w-0">
              <div class="flex flex-wrap items-center gap-2">
                <h3 class="font-semibold">{service.name}</h3>
                {#if service.credential_configured}
                  <span class="status-pill ready"
                    ><CheckCircle2 size={13} /> Connected</span
                  >{:else}<span class="status-pill">Credential needed</span
                  >{/if}
              </div>
              <p class="muted mt-2 break-all text-xs">{service.api_base}</p>
              <div class="muted mt-3 flex flex-wrap gap-x-4 gap-y-1 text-xs">
                <span>{service.model}</span><span
                  >{timestampLabel(service)}</span
                ><span
                  >{service.diarization
                    ? 'Diarization'
                    : 'Single-speaker timing'}</span
                >
              </div>
            </div>
            <div class="flex shrink-0 gap-2">
              <button
                class="btn btn-icon btn-secondary"
                aria-label={`Edit ${service.name}`}
                onclick={() => openSettings(service)}
                ><Settings2 size={17} /></button
              ><button
                class="btn btn-icon btn-secondary"
                aria-label={`Remove ${service.name}`}
                disabled={busy === service.id}
                onclick={() => removeService(service)}
                ><Trash2 size={17} /></button
              >
            </div>
          </div>
        </article>
      {/each}
    </div>
  {:else}
    <div class="surface muted mt-6 p-6 text-sm">
      No cloud recognizer is connected yet. Choose a profile below.
    </div>
  {/if}

  <details class="surface mt-6 p-5" open={!payload.services.length}>
    <summary class="cursor-pointer font-semibold"
      >Compatible service profiles</summary
    >
    <div class="mt-4 grid gap-4 lg:grid-cols-2">
      {#each payload.profiles as profile}
        <div class="rounded-xl border border-[var(--line)] p-4">
          <div class="flex items-start justify-between gap-4">
            <div>
              <div class="font-semibold">{profile.name}</div>
              <p class="muted mt-1 text-xs leading-relaxed">
                {profile.description}
              </p>
            </div>
            <button
              class="btn btn-sm btn-secondary shrink-0"
              disabled={busy === profile.id}
              onclick={() => useProfile(profile)}
              >{payload.services.some(
                (item) => serviceId(item.id) === serviceId(profile.id)
              )
                ? 'Reset profile'
                : 'Use profile'}</button
            >
          </div>
          <div class="muted mt-3 flex flex-wrap gap-x-4 gap-y-1 text-xs">
            <span>{profile.model}</span><span>{timestampLabel(profile)}</span>
          </div>
          {#if profile.source_url}<a
              class="mt-3 inline-flex items-center gap-1 text-xs font-semibold text-[var(--accent)]"
              href={profile.source_url}
              target="_blank"
              rel="noreferrer">Documentation <ExternalLink size={12} /></a
            >{/if}
        </div>
      {/each}
    </div>
  </details>
</section>

{#if editing}
  <div
    class="fixed inset-0 z-50 grid place-items-center bg-black/40 p-2 sm:p-5"
  >
    <div
      class="surface modal-panel flex w-full max-w-2xl flex-col"
      use:modalFocus={{ onclose: () => (editing = null) }}
    >
      <div class="modal-scroll p-5 sm:p-7">
        <div class="flex items-start justify-between gap-4">
          <div>
            <div class="eyebrow">Recognition connection</div>
            <h2 class="mt-1 text-2xl font-semibold">{editing.name}</h2>
          </div>
          <button
            class="btn btn-icon btn-secondary"
            aria-label="Close recognition settings"
            onclick={() => (editing = null)}><X size={19} /></button
          >
        </div>

        <label class="mt-6 block text-sm font-semibold"
          >Base URL<input bind:value={editing.api_base} class="field" /></label
        ><label class="mt-4 block text-sm font-semibold"
          >Model<input bind:value={editing.model} class="field" /></label
        >
        {#if serviceId(editing.id) === 'azure_mai_transcribe_1_5'}
          <label class="mt-4 block text-sm font-semibold"
            >Transcript style<select
              value={String(
                editing.settings?.stt_transcribe_style ?? 'readability'
              )}
              onchange={(event) =>
                setEditingSetting(
                  'stt_transcribe_style',
                  event.currentTarget.value
                )}
              class="field"
              ><option value="readability">Readable transcript</option><option
                value="verbatim">Verbatim · preserve fillers</option
              ></select
            ></label
          >
          <fieldset class="mt-5 rounded-2xl border border-[var(--line)] p-4">
            <legend class="px-1 text-sm font-semibold">Long recordings</legend>
            <p class="muted mt-1 text-xs leading-relaxed">
              Files beyond the chunk limit are split without removing audio.
              Pandrator prefers a long silence near each boundary, then falls
              back to the lowest-energy window.
            </p>
            <div class="mt-4 grid gap-4 sm:grid-cols-3">
              <label class="text-xs font-semibold"
                >Maximum chunk (minutes)<input
                  class="field"
                  type="number"
                  min="10"
                  max="110"
                  step="5"
                  value={Number(
                    editing.settings?.stt_cloud_max_chunk_seconds ?? 5400
                  ) / 60}
                  oninput={(event) =>
                    setEditingSetting(
                      'stt_cloud_max_chunk_seconds',
                      Number(event.currentTarget.value) * 60
                    )}
                /></label
              ><label class="text-xs font-semibold"
                >Boundary search (minutes)<input
                  class="field"
                  type="number"
                  min="1"
                  max="15"
                  step="1"
                  value={Number(
                    editing.settings?.stt_cloud_chunk_search_seconds ?? 300
                  ) / 60}
                  oninput={(event) =>
                    setEditingSetting(
                      'stt_cloud_chunk_search_seconds',
                      Number(event.currentTarget.value) * 60
                    )}
                /></label
              ><label class="text-xs font-semibold"
                >Preferred silence (seconds)<input
                  class="field"
                  type="number"
                  min="0.5"
                  max="10"
                  step="0.25"
                  value={Number(
                    editing.settings?.stt_cloud_min_silence_ms ?? 1500
                  ) / 1000}
                  oninput={(event) =>
                    setEditingSetting(
                      'stt_cloud_min_silence_ms',
                      Number(event.currentTarget.value) * 1000
                    )}
                /></label
              >
            </div>
          </fieldset>
        {/if}
        <div class="mt-5">
          <CredentialStorageFields
            backends={credentialBackends}
            bind:backend={editingCredentialBackend}
            bind:reference={editingCredentialReference}
            bind:secret={editingApiKey}
            bind:deletePrevious={deletePreviousCredential}
            configured={Boolean(editing.credential_configured)}
            currentSource={editing.credential_source ?? 'none'}
            existingBackend={editingExistingCredentialBackend}
            suggestedEnvironment={editing.api_key_env ?? ''}
            secretLabel={serviceId(editing.id) === 'azure_mai_transcribe_1_5'
              ? 'Azure Speech key'
              : 'OpenAI API key'}
          />
        </div>
        {#if editing.credential_configured || editing.secret_ref}
          <label class="mt-3 flex items-center gap-2 text-sm"
            ><input
              type="checkbox"
              bind:checked={removeCredential}
              class="accent-[var(--accent)]"
            /> Remove this connection's credential reference</label
          >
        {/if}
      </div>
      <footer
        class="flex shrink-0 justify-end gap-2 border-t border-[var(--line)] px-5 py-4 sm:px-7"
      >
        <button class="btn btn-secondary" onclick={() => (editing = null)}
          >Cancel</button
        ><button
          class="btn btn-primary"
          disabled={busy === editing.id}
          onclick={saveEditing}>Save recognition connection</button
        >
      </footer>
    </div>
  </div>
{/if}
