<script lang="ts">
  import { LoaderCircle, WandSparkles } from '@lucide/svelte';
  import { onDestroy, onMount } from 'svelte';
  import { voiceApi } from './admin-api';
  import { jobApi } from './domain-api';
  import type { GoogleVoiceDesignReceipt, TtsService } from './api-models';
  import { errorMessage } from './errors';
  import { ApiError } from './api';
  import LanguageSelect from './LanguageSelect.svelte';
  import AudioPlayer from './AudioPlayer.svelte';

  let {
    service,
    initialPrompt = '',
    initialCategory = '',
    onbusychange,
    onqueued,
    oncreated,
    ondone
  }: {
    service: TtsService;
    initialPrompt?: string;
    initialCategory?: string;
    onbusychange: (busy: boolean) => void;
    onqueued: (voiceId: string) => void;
    oncreated: (receipt: GoogleVoiceDesignReceipt) => void;
    ondone: () => void;
  } = $props();
  const models = ['gemini-3.8-flash-tts', 'gemini-3.8-flash-lite-tts'] as const;
  let model = $state<(typeof models)[number]>(models[0]);
  let name = $state('');
  let prompt = $state('');
  let language = $state('en');
  let category = $state<'male' | 'female' | 'androgynous' | 'unspecified'>(
    'unspecified'
  );
  let busy = $state(false);
  let requested = $state(false);
  let jobId = $state('');
  let receipt = $state<GoogleVoiceDesignReceipt | null>(null);
  let error = $state('');
  let detail = $state('');
  let idempotencyKey = crypto.randomUUID();
  let alive = true;
  const validLanguage = $derived(
    /^[a-zA-Z]{2,3}(?:-[a-zA-Z0-9]{2,8})*$/.test(language)
  );
  const configured = $derived(
    service.credential_configured === true && service.available !== false
  );

  async function createVoice() {
    if (
      busy ||
      receipt ||
      !name.trim() ||
      !prompt.trim() ||
      !validLanguage ||
      !configured
    )
      return;
    busy = true;
    onbusychange(true);
    error = '';
    requested = true;
    detail = 'Saving the creation request…';
    try {
      if (!jobId) {
        const queued = await voiceApi.designGoogle(
          {
            service_id: service.id === 'vertex_ai' ? 'vertex_ai' : 'gemini',
            model,
            name: name.trim(),
            prompt: prompt.trim(),
            language,
            voice_category: category
          },
          idempotencyKey
        );
        jobId = queued.id;
        onqueued(queued.voice_id);
      }
      for (let attempt = 0; attempt < 600; attempt += 1) {
        if (!alive) return;
        const job = await jobApi.get(jobId);
        detail = job.progress_detail || 'Google is creating your voice…';
        if (job.status === 'succeeded') {
          const result = job.result_json ?? {};
          if (!result.voice_id || !result.provider_voice_id)
            throw new Error(
              'The creation job did not return its saved voice identity. Check Activity & logs.'
            );
          receipt = {
            voice_id: String(result.voice_id),
            provider_voice_id: String(result.provider_voice_id),
            service_id: service.id,
            model,
            artifact_id: result.artifact_id
              ? String(result.artifact_id)
              : undefined,
            warning: result.warning ? String(result.warning) : undefined
          };
          oncreated(receipt);
          return;
        }
        if (['failed', 'canceled', 'interrupted'].includes(job.status)) {
          throw new Error(
            job.error_message ||
              `Voice creation ${job.status}. Check the saved request in Activity & logs before creating another voice.`
          );
        }
        await new Promise((resolve) => setTimeout(resolve, 500));
      }
      throw new Error(
        'Voice creation is still running. Check its progress in Activity & logs.'
      );
    } catch (caught) {
      if (alive) {
        error = errorMessage(caught);
        if (
          !jobId &&
          caught instanceof ApiError &&
          [400, 401, 403, 409, 422].includes(caught.status) &&
          caught.code !== 'idempotency_in_progress'
        ) {
          requested = false;
          idempotencyKey = crypto.randomUUID();
        }
      }
    } finally {
      if (alive) {
        busy = false;
        onbusychange(false);
      }
    }
  }
  onMount(() => {
    prompt = initialPrompt;
    if (
      initialCategory === 'male' ||
      initialCategory === 'female' ||
      initialCategory === 'androgynous'
    ) {
      category = initialCategory;
    }
  });
  onDestroy(() => {
    alive = false;
  });
</script>

<div class="mt-5 space-y-4">
  <p class="muted text-sm leading-relaxed">
    Create one voice in your Google project and save it immediately in the Voice
    Library. Google supplies an audition with its own text. The saved voice
    works with both Gemini 3.8 TTS models.
  </p>
  {#if !configured}
    <p
      role="status"
      class="rounded-xl border border-[var(--warning)]/40 p-3 text-sm"
    >
      Configure {service.name} credentials in
      <a
        href="/providers?tab=speech"
        class="font-semibold text-[var(--accent)] underline"
        >speech provider settings</a
      > before creating a voice.
    </p>
  {/if}
  {#if receipt}
    <div role="status" class="rounded-xl border border-[var(--accent)] p-4">
      <h3 class="font-semibold">{name} is saved</h3>
      <p class="muted mt-2 text-sm">
        Its description, Google identity and project association are stored in
        your Voice Library.
      </p>
      <p class="mt-2 break-all font-mono text-xs">
        {receipt.provider_voice_id}
      </p>
    </div>
    {#if receipt.artifact_id}
      <AudioPlayer
        src={`/api/v1/artifacts/${receipt.artifact_id}/content`}
        label="Google voice audition"
      />
    {/if}
    {#if receipt.warning}<p role="alert" class="text-sm">
        {receipt.warning}
      </p>{/if}
    <button class="btn btn-primary" onclick={ondone}>Open saved voice</button>
  {:else}
    <div class="grid gap-4 sm:grid-cols-2">
      <label class="text-sm font-semibold"
        >Voice name
        <input
          bind:value={name}
          disabled={busy || requested}
          maxlength="255"
          placeholder="e.g. Warm storyteller"
          class="mt-1 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2.5 font-normal"
        />
      </label>
      <label class="text-sm font-semibold"
        >{service.id === 'vertex_ai' ? 'Use with model' : 'Design model'}
        <select
          bind:value={model}
          disabled={busy || requested}
          class="mt-1 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2.5 font-normal"
        >
          {#each models.filter((id) => service.models?.includes(id)) as id}
            <option value={id}
              >{id === models[0]
                ? 'Gemini 3.8 Flash TTS'
                : 'Gemini 3.8 Flash-Lite TTS'}</option
            >
          {/each}
        </select>
      </label>
      <LanguageSelect
        bind:value={language}
        label="Voice language"
        allowCustom={true}
        disabled={busy || requested}
      />
      <label class="text-sm font-semibold"
        >Voice category
        <select
          bind:value={category}
          disabled={busy || requested}
          class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-3 font-normal"
        >
          <option value="unspecified">Unspecified</option><option value="female"
            >Female</option
          ><option value="male">Male</option><option value="androgynous"
            >Androgynous</option
          >
        </select>
        <span class="muted mt-1 block text-xs font-normal"
          >A library label. Describe the desired vocal traits below.</span
        >
      </label>
    </div>
    <label class="block text-sm font-semibold"
      >Voice description
      <textarea
        bind:value={prompt}
        disabled={busy || requested}
        maxlength="4000"
        rows="4"
        placeholder="A warm, thoughtful storyteller in her 50s, with a gentle Polish accent and a clear, softly textured voice."
        class="mt-1 w-full resize-y rounded-xl border border-[var(--line)] bg-[var(--paper)] p-3 font-normal leading-relaxed"
      ></textarea>
      <span class="muted mt-1 block text-xs font-normal"
        >Define lasting traits such as age, timbre and accent. Add emotion for
        each passage later through Speech direction.</span
      >
    </label>
    <p class="muted text-xs leading-relaxed">
      Google generates an audition during creation; token charges may apply.
      {#if service.id === 'vertex_ai'}Vertex creates the voice at Flash TTS
        rates.{:else}Google's Developer API pricing does not list a separate
        voice-design charge.{/if}
      Stored voices expire one year after their last use; using them renews that period.
      Google allows 200 stored voices per project.
    </p>
    {#if error}<p
        role="alert"
        class="rounded-xl border border-red-400/40 p-3 text-sm text-red-600"
      >
        {error}
      </p>{/if}
    {#if busy}<p role="status" class="flex items-center gap-2 text-sm">
        <LoaderCircle size={16} class="animate-spin" />{detail}
      </p>{/if}
    <div
      class="flex flex-wrap items-center justify-end gap-3 border-t border-[var(--line)] pt-4"
    >
      <button
        class="btn btn-primary"
        onclick={createVoice}
        disabled={busy ||
          !configured ||
          !name.trim() ||
          !prompt.trim() ||
          !validLanguage}
      >
        <WandSparkles size={16} />{busy
          ? 'Creating…'
          : requested
            ? 'Check creation request'
            : 'Create & save voice'}
      </button>
    </div>
  {/if}
</div>
