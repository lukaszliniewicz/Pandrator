<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import { diagnosticsApi } from './admin-api';
  import { sttLanguageProblem } from './stt-language-policy';
  import type { RuntimeCapabilities } from './api-models';
  import { errorMessage } from './errors';
  import { ExternalLink, RotateCcw, Save } from '@lucide/svelte';
  import { sessionApi } from './domain-api';
  import type { GlobalDefaultsPayload } from './api-models';
  import SettingField from './SettingField.svelte';
  import TtsServiceSelect from './TtsServiceSelect.svelte';
  import {
    compareSettingOrder,
    GLOBAL_TTS_KEYS,
    optionsFor,
    settingApplies,
    settingLabel
  } from './settings-fields';

  let {
    section,
    workspaceId,
    workspaceName = 'your projects'
  }: {
    section: string;
    workspaceId?: string;
    workspaceName?: string;
  } = $props();
  let active = true;
  let sttCapabilities = $state<RuntimeCapabilities>({});
  let payload = $state<GlobalDefaultsPayload | null>(null);
  let value = $state<Record<string, unknown>>({});
  const sttEffective = $derived({ ...payload?.effective, ...value });
  const languageProblem = $derived(
    section === 'stt'
      ? sttLanguageProblem(
          sttCapabilities,
          String(sttEffective.stt_engine || ''),
          sttEffective.stt_language
        )
      : ''
  );
  let saving = $state(false);
  let message = $state('');
  const canManage = $derived(payload?.can_manage === true);
  function displayValue(key: string, item: unknown) {
    const choice = optionsFor(section, key)?.find(
      (option) => String(option.value) === String(item)
    );
    if (choice) return choice.label;
    if (item === null || item === undefined || item === '') return 'Automatic';
    if (typeof item === 'boolean') return item ? 'On' : 'Off';
    return typeof item === 'object' ? JSON.stringify(item) : String(item);
  }

  const providerSetting = (key: string) =>
    key === 'provider_configs' ||
    key === 'use_external_server' ||
    key === 'external_server_url' ||
    key === 'openai_audio_endpoint' ||
    key.endsWith('_base_url') ||
    key.endsWith('_api_key');
  const entries = $derived(
    Object.entries(payload?.effective ?? {})
      .filter(([key]) => {
        if (section === 'output' && key === 'cover_artifact_id') return false;
        if (!settingApplies(section, key, { ...payload?.effective, ...value }))
          return false;
        if (section !== 'tts') return true;
        if (providerSetting(key)) return false;
        return GLOBAL_TTS_KEYS.has(key);
      })
      .sort(([left], [right]) => compareSettingOrder(section, left, right))
  );

  const current = (key: string, fallback: unknown) =>
    Object.prototype.hasOwnProperty.call(value, key) ? value[key] : fallback;
  const set = (key: string, next: unknown) =>
    (value = { ...value, [key]: next });

  async function load() {
    message = '';
    const result = await sessionApi.defaults(section, { workspaceId });
    if (!active) return;
    payload = result;
    value = { ...(payload.value ?? {}) };
    if (section === 'stt' && result.can_manage)
      void diagnosticsApi
        .capabilities()
        .then((result) => {
          if (active) sttCapabilities = result;
        })
        .catch(() => {});
  }

  async function save() {
    if (!canManage) return;
    if (languageProblem) {
      message = languageProblem;
      return;
    }
    saving = true;
    message = '';
    try {
      const currentPayload = payload;
      if (!currentPayload) return;
      if (section === 'tts') {
        value = Object.fromEntries(
          Object.entries(value).filter(([key]) => !providerSetting(key))
        );
      }
      const result = await sessionApi.saveDefaults(
        section,
        currentPayload.revision,
        value,
        workspaceId
      );
      if (!active) return;
      payload = {
        ...currentPayload,
        revision: result.revision,
        value: result.value,
        effective: { ...currentPayload.builtin, ...result.value }
      };
      message = 'Saved.';
    } catch (caught) {
      if (active) message = errorMessage(caught);
    } finally {
      if (active) saving = false;
    }
  }

  async function restoreBuiltins() {
    if (!canManage) return;
    saving = true;
    message = '';
    try {
      const currentPayload = payload;
      if (!currentPayload) return;
      const result = await sessionApi.saveDefaults(
        section,
        currentPayload.revision,
        {},
        workspaceId
      );
      if (!active) return;
      payload = {
        ...currentPayload,
        revision: result.revision,
        value: {},
        effective: { ...currentPayload.builtin }
      };
      value = {};
      message = 'Restored built-in defaults.';
    } catch (caught) {
      if (active) message = errorMessage(caught);
    } finally {
      if (active) saving = false;
    }
  }

  onMount(() => {
    void load().catch((caught) => {
      if (active) message = errorMessage(caught);
    });
  });
  onDestroy(() => {
    active = false;
  });
</script>

<div class="mt-4 border-t border-[var(--line)] pt-4">
  {#if message}<p class="muted mb-3 text-sm" role="status">{message}</p>{/if}
  {#if section === 'tts' && canManage}
    <div
      class="mb-4 flex flex-wrap items-center justify-between gap-3 rounded-xl bg-[var(--accent-soft)] p-3"
    >
      <p class="text-xs">
        These are generation defaults. Endpoint URLs, credentials, and provider
        catalogues are managed separately.
      </p>
      <a href="/providers?tab=tts" class="btn btn-sm btn-secondary"
        ><ExternalLink size={14} /> Manage TTS services</a
      >
    </div>
  {/if}
  {#if payload}
    {#if !canManage}
      <p class="muted mb-4 text-sm">
        Defaults for {workspaceName}. Workspace managers can change them.
      </p>
      <dl class="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
        {#each entries as [key, fallback]}
          <div class="min-w-0 rounded-lg border border-[var(--line)] p-3">
            <dt class="text-xs font-semibold">{settingLabel(key)}</dt>
            <dd class="muted mt-1 break-words text-sm">
              {displayValue(key, current(key, fallback))}
            </dd>
          </div>
        {/each}
      </dl>
    {:else}
      <div class="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
        {#each entries as [key, fallback]}
          {#if section === 'tts' && key === 'service'}<TtsServiceSelect
              {workspaceId}
              value={String(current(key, fallback) ?? '')}
              onchange={(next, resetSelection) => {
                set(key, next);
                if (resetSelection) {
                  for (const field of [
                    'model',
                    'xtts_model',
                    'voice',
                    'speaker'
                  ])
                    set(field, '');
                }
              }}
            />{:else}<SettingField
              {section}
              keyName={key}
              {sttCapabilities}
              sourceLanguage={sttEffective.stt_language}
              value={current(key, fallback)}
              onchange={(next) => set(key, next)}
              compact
            />{/if}
        {/each}
      </div>
      {#if languageProblem}<p class="mt-3 text-sm text-red-600" role="alert">
          {languageProblem}
        </p>{/if}
      <div class="mt-4 flex flex-wrap items-center gap-3">
        <button
          onclick={restoreBuiltins}
          disabled={saving || !Object.keys(value).length}
          class="btn btn-sm btn-secondary"
          ><RotateCcw size={13} /> Restore built-in defaults</button
        ><button
          onclick={save}
          disabled={saving || Boolean(languageProblem)}
          class="btn btn-sm btn-primary"
          ><Save size={13} />{saving
            ? 'Saving…'
            : 'Save workspace defaults'}</button
        >
      </div>
    {/if}
  {:else if !message}<p class="muted text-sm" role="status">
      Loading defaults…
    </p>
  {/if}
</div>
