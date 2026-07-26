<script lang="ts">
  import { ChevronDown, ExternalLink, Globe2, RotateCcw, Save, ShieldCheck, TriangleAlert } from '@lucide/svelte';
  import { sessionApi } from './domain-api';
  import type { SettingsPayload } from './api-models';
  import SettingField from './SettingField.svelte';

  let { sessionId, section, title, description = '' }: { sessionId: string; section: string; title: string; description?: string } = $props();
  let payload = $state<SettingsPayload|null>(null);
  let override = $state<Record<string, unknown>>({});
  let advanced = $state(false);
  let saving = $state(false);
  let message = $state('');
  const common: Record<string, string[]> = { text: ['enable_sentence_splitting', 'max_sentence_length', 'enable_sentence_appending', 'enable_nemo_normalization', 'normalize_all_caps', 'llm_tts_document_optimization', 'llm_tts_optimization', 'llm_tts_document_batch_size', 'llm_tts_batch_size'], stt: ['stt_engine', 'stt_model_quantization', 'stt_compute_backend', 'stt_compute_device', 'stt_language', 'whisper_prompt', 'moss_max_chunk_seconds', 'moss_vad_enabled', 'moss_ctc_alignment_enabled', 'moss_ctc_padding_seconds', 'crispasr_vad_enabled', 'crispasr_vad_threshold', 'crispasr_vad_min_speech_ms', 'crispasr_vad_min_silence_ms', 'crispasr_vad_max_speech_seconds', 'crispasr_vad_speech_pad_ms', 'diarization_enabled'], subtitles: ['max_lines', 'max_chars_per_line', 'max_cps', 'min_duration_ms', 'max_duration_ms', 'min_gap_ms', 'phrase_gap_ms', 'boundary_correction_enabled', 'merge_threshold_ms'], correction: ['enabled', 'model_name', 'instructions', 'preserve_timing', 'max_subtitles_per_call', 'context_before', 'context_after', 'web_research_enabled', 'web_research_provider', 'web_research_language', 'web_research_max_searches', 'web_research_max_extractions', 'web_research_preferred_domains', 'web_research_blocked_domains', 'request_timeout_seconds'], translation: ['enabled', 'backend', 'source_language', 'target_language', 'professional_cleanup', 'model_name', 'instructions', 'glossary', 'glossary_enabled', 'context', 'max_subtitles_per_call', 'max_line_length', 'no_remove_subtitles', 'web_research_enabled', 'web_research_provider', 'web_research_language', 'web_research_max_searches', 'web_research_max_extractions', 'web_research_preferred_domains', 'web_research_blocked_domains', 'request_timeout_seconds'], tts: ['service', 'model', 'voice', 'language', 'speed', 'max_attempts'], audio: ['audio_verification_mode', 'sentence_silence_ms', 'paragraph_silence_ms', 'fade_enabled', 'fade_in_ms', 'fade_out_ms', 'synchronization_delay_ms', 'synchronization_speed', 'synchronization_sentence_gap_ms'], rvc: ['enabled', 'model', 'pitch', 'f0_method', 'filter_radius', 'index_rate', 'volume_envelope', 'protect'], source_cleaning: ['agentic', 'max_iterations', 'pdf_ocr_mode', 'pdf_ocr_language', 'pdf_ocr_dpi', 'pdf_remove_toc', 'pdf_remove_repeated_marginals', 'request_timeout_seconds'], output: ['format', 'bitrate', 'export_mode', 'audio_mode', 'subtitle_mode', 'subtitle_selection', 'subtitle_format', 'burn_video_encoder', 'burn_video_resolution', 'burn_video_quality', 'burn_video_speed', 'burn_audio_codec', 'burn_audio_bitrate', 'title', 'artist', 'album', 'genre', 'language'] };
  common.text.splice(7, 0, 'speech_optimization_mode', 'speech_plan_save_proposals', 'tts_optimization_model', 'llm_concurrent_calls');
  const value = (key: string, fallback: unknown) => Object.prototype.hasOwnProperty.call(override, key) ? override[key] : fallback;
  const set = (key: string, next: unknown) => override = { ...override, [key]: next };
  const entries = $derived(Object.entries(payload?.effective ?? {}).sort(([left], [right]) => { const order = common[section] ?? []; const li = order.indexOf(left), ri = order.indexOf(right); return (li < 0 ? 999 : li) - (ri < 0 ? 999 : ri) || left.localeCompare(right); }));
  const providerSetting = (key: string) => key === 'provider_configs' || key === 'use_external_server' || key === 'external_server_url' || key === 'openai_audio_endpoint' || key.endsWith('_base_url') || key.endsWith('_api_key');
  const sectionName = (value: string) => ({ tts: 'TTS', stt: 'STT', rvc: 'RVC' } as Record<string, string>)[value] ?? value.replaceAll('_', ' ');
  const applicable = $derived(entries.filter(([key]) => { if (section !== 'tts') return true; if (providerSetting(key)) return false; const service = String(value('service', payload?.effective?.service ?? '')).toLowerCase(); if (key.startsWith('voxcpm_')) return service.includes('voxcpm'); if (key.startsWith('fishs2_')) return service.includes('fish'); if (key.startsWith('voxtral_')) return service.includes('voxtral'); if (key.startsWith('silero_')) return service.includes('silero'); if (key.startsWith('chatterbox_')) return service.includes('chatterbox'); if (key.startsWith('xtts_') || ['temperature', 'length_penalty', 'repetition_penalty', 'top_k', 'top_p', 'do_sample', 'num_beams', 'enable_text_splitting', 'stream_chunk_size', 'gpt_cond_len', 'gpt_cond_chunk_len', 'max_ref_len', 'sound_norm_refs', 'overlap_wav_len'].includes(key)) return service.includes('xtts'); if (key.startsWith('openai_audio_')) return service.includes('openai') || service.includes('gemini') || service.includes('custom'); return true; }));
  const visible = $derived(applicable.filter(([key]) => advanced || (common[section] ?? []).includes(key)));
  const stageResearchSection = $derived(section === 'correction' || section === 'translation');
  const standardVisible = $derived(visible.filter(([key]) => !key.startsWith('web_research_')));
  const researchVisible = $derived(
    applicable.filter(([key]) =>
      key.startsWith('web_research_')
      && key !== 'web_research_enabled'
      && (advanced || (common[section] ?? []).includes(key))
    )
  );
  const researchEnabled = $derived(Boolean(value('web_research_enabled', payload?.effective?.web_research_enabled ?? false)));
  const deepLResearchConflict = $derived(
    section === 'translation'
    && String(value('backend', payload?.effective?.backend ?? 'llm')).toLowerCase() === 'deepl'
    && researchEnabled
  );
  const deterministicText = $derived(applicable.filter(([key]) => !key.startsWith('llm_')).filter(([key]) => advanced || ['enable_sentence_splitting','max_sentence_length','enable_sentence_appending','enable_nemo_normalization','normalize_all_caps'].includes(key)));
  const llmText = $derived(applicable.filter(([key]) => key.startsWith('llm_') || key.startsWith('speech_') || ['tts_optimization_model','combined_prompt','first_prompt','second_prompt','third_prompt'].includes(key)).filter(([key]) => {
    if (['llm_tts_optimization','llm_tts_document_optimization'].includes(key)) return true;
    if (!Boolean(value('llm_tts_optimization', payload?.effective?.llm_tts_optimization)) && !Boolean(value('llm_tts_document_optimization', payload?.effective?.llm_tts_document_optimization))) return false;
    const planningMode=String(value('speech_optimization_mode',payload?.effective?.speech_optimization_mode??'guarded'));
    if (key==='speech_plan_min_retention') return planningMode==='flexible';
    if (['llm_multi_stage','combined_prompt','first_prompt','second_prompt','third_prompt'].includes(key) && planningMode!=='legacy') return false;
    if (key==='speech_plan_save_proposals' && planningMode==='legacy') return false;
    const divided=Boolean(value('llm_multi_stage', payload?.effective?.llm_multi_stage));
    if (['first_prompt','second_prompt','third_prompt'].includes(key)) return divided;
    if (key === 'combined_prompt') return !divided;
    return true;
  }));
  async function load() { payload = await sessionApi.settings(sessionId,section); override = { ...(payload.override ?? {}) }; }
  async function save() { saving = true; message = ''; try { if (section === 'tts') override = Object.fromEntries(Object.entries(override).filter(([key]) => !providerSetting(key))); payload = await sessionApi.saveSettings(sessionId,section,payload!.revision,override); override = { ...payload.override }; message = 'Saved for this session.'; } catch (caught) { message = caught instanceof Error ? caught.message : String(caught); } finally { saving = false; } }
  async function reset() { saving = true; message = ''; try { payload = await sessionApi.saveSettings(sessionId,section,payload!.revision,{}); override = {}; message = 'Reverted to application defaults.'; } catch (caught) { message = caught instanceof Error ? caught.message : String(caught); } finally { saving = false; } }
  async function saveAsDefaults() {
    saving = true; message = '';
    try {
      const promoted = section === 'tts' ? Object.fromEntries(Object.entries(override).filter(([key]) => !providerSetting(key))) : override;
      const defaults = await sessionApi.defaults(section);
      await sessionApi.saveDefaults(section,defaults.revision,{ ...(defaults.value ?? {}), ...promoted });
      payload = await sessionApi.saveSettings(sessionId,section,payload!.revision,Object.fromEntries(Object.entries(override).filter(([key]) => !Object.prototype.hasOwnProperty.call(promoted, key))));
      override = { ...payload.override };
      message = 'Saved as application defaults.';
    } catch (caught) { message = caught instanceof Error ? caught.message : String(caught); }
    finally { saving = false; }
  }
  load();
</script>

<section class="surface rounded-2xl p-5">
  <div class="flex flex-wrap items-start justify-between gap-4">
    <div><div class="eyebrow">{sectionName(section)}</div><h2 class="mt-1 text-xl font-semibold">{title}</h2>{#if description}<p class="muted mt-2 max-w-2xl text-sm">{description}</p>{/if}</div>
    <div class="flex flex-wrap gap-2">{#if section === 'tts'}<a href="/providers?tab=tts" class="tool"><ExternalLink size={14}/> TTS services</a>{/if}<button onclick={reset} disabled={saving || !Object.keys(override).length} class="tool"><RotateCcw size={14}/> Revert to defaults</button><button onclick={saveAsDefaults} disabled={saving || deepLResearchConflict || !Object.keys(override).length} title={deepLResearchConflict ? 'Resolve the translation backend and web research conflict first.' : ''} class="tool"><Save size={14}/> Save as defaults</button><button onclick={save} disabled={saving || deepLResearchConflict} title={deepLResearchConflict ? 'Resolve the translation backend and web research conflict first.' : ''} class="tool bg-[var(--accent)] text-white"><Save size={14}/> {saving ? 'Saving…' : 'Save'}</button></div>
  </div>
  {#if payload}
    {#if section==='text'}
      <div class="mt-5 grid gap-5 xl:grid-cols-2">
        <section class="rounded-2xl border border-[var(--line)] p-4"><div class="text-sm font-semibold">Segmentation and deterministic processing</div><p class="muted mt-1 text-xs leading-relaxed">Creates generation units, paragraph boundaries, and predictable text normalization without an LLM or provider cost.</p><div class="mt-4 grid gap-4 sm:grid-cols-2">{#each deterministicText as [key,fallback]}<div><SettingField {section} keyName={key} value={value(key,fallback)} onchange={(next)=>set(key,next)} compact/>{#if Object.prototype.hasOwnProperty.call(override,key)}<span class="mt-1 block text-[.65rem] text-[var(--accent)]">Session override</span>{:else}<span class="muted mt-1 block text-[.65rem]">Inherited</span>{/if}</div>{/each}</div></section>
        <section class="rounded-2xl border border-[var(--line)] bg-[var(--accent-soft)] p-4"><div class="flex items-center gap-2 text-sm font-semibold"><ShieldCheck size={16}/> Optional LLM speech planning</div><p class="muted mt-1 text-xs leading-relaxed">Guarded mode asks for typed decisions over stable spans; flexible mode may revise the complete speech sentence behind protected placeholders. Both preserve display text and validate the result before synthesis.</p><div class="mt-4 grid gap-4">{#each llmText as [key,fallback]}<div><SettingField {section} keyName={key} value={value(key,fallback)} onchange={(next)=>set(key,next)} compact/><span class="mt-1 block text-[.65rem] text-[var(--accent)]" class:muted={!Object.prototype.hasOwnProperty.call(override,key)}>{Object.prototype.hasOwnProperty.call(override,key)?'Session override':'Inherited'}</span></div>{/each}</div>{#if researchEnabled}<p class="muted mt-3 text-[.68rem]">Web research settings belong to correction and translation; speech planning uses only the reviewed pronunciation library.</p>{/if}</section>
      </div>
      <button onclick={() => advanced = !advanced} class="muted mt-5 flex items-center gap-1 text-xs font-semibold"><ChevronDown class={advanced ? 'rotate-180' : ''} size={14}/>{advanced ? 'Hide' : 'Show'} advanced deterministic settings</button>
    {:else if stageResearchSection}
      <div class="mt-5 grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
        {#each standardVisible as [key, fallback]}
          <div>
            <SettingField {section} keyName={key} value={value(key, fallback)} onchange={(next) => set(key, next)} compact/>
            {#if Object.prototype.hasOwnProperty.call(override, key)}<span class="mt-1 block text-[.65rem] text-[var(--accent)]">Session override</span>{:else}<span class="muted mt-1 block text-[.65rem]">Inherited</span>{/if}
          </div>
        {/each}
      </div>
      <section class:enabled={researchEnabled} class="research-card mt-5">
        <div class="flex flex-wrap items-start gap-4">
          <span class="grid size-10 shrink-0 place-items-center rounded-xl bg-[var(--accent-soft)] text-[var(--accent)]"><Globe2 size={19}/></span>
          <div class="min-w-0 flex-1"><div class="text-sm font-semibold">Bounded web research</div><p class="muted mt-1 max-w-3xl text-xs leading-relaxed">Before {section}, the selected LLM may ask Jina to search and extract a small number of pages. Pandrator keeps a source ledger, rejects unsourced evidence, and treats page content as untrusted.</p></div>
          <a href="/providers?tab=credentials" class="tool"><ExternalLink size={14}/> Jina API key</a>
        </div>
        <div class="mt-4 max-w-sm"><SettingField {section} keyName="web_research_enabled" value={researchEnabled} onchange={(next) => set('web_research_enabled', next)} compact/><span class="mt-1 block text-[.65rem] text-[var(--accent)]" class:muted={!Object.prototype.hasOwnProperty.call(override,'web_research_enabled')}>{Object.prototype.hasOwnProperty.call(override,'web_research_enabled')?'Session override':'Inherited'}</span></div>
        {#if deepLResearchConflict}<div class="mt-4 flex gap-2 rounded-xl bg-amber-500/10 p-3 text-xs text-amber-700"><TriangleAlert class="mt-0.5 shrink-0" size={14}/>Web research can augment the LLM translation backend only. Choose LLM translation or turn research off before saving.</div>{/if}
        {#if researchEnabled}
          <div class="mt-5 grid gap-4 border-t border-[var(--line)] pt-5 sm:grid-cols-2 xl:grid-cols-3">
            {#each researchVisible as [key, fallback]}
              <div><SettingField {section} keyName={key} value={value(key, fallback)} onchange={(next) => set(key, next)} compact/><span class="mt-1 block text-[.65rem] text-[var(--accent)]" class:muted={!Object.prototype.hasOwnProperty.call(override,key)}>{Object.prototype.hasOwnProperty.call(override,key)?'Session override':'Inherited'}</span></div>
            {/each}
          </div>
        {/if}
      </section>
      {#if applicable.length > (common[section]?.length ?? 0)}<button onclick={() => advanced = !advanced} class="muted mt-5 flex items-center gap-1 text-xs font-semibold"><ChevronDown class={advanced ? 'rotate-180' : ''} size={14}/>{advanced ? 'Hide' : 'Show'} advanced settings and research budgets</button>{/if}
    {:else}
      <div class="mt-5 grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
        {#each visible as [key, fallback]}
          <div>
            <SettingField {section} keyName={key} value={value(key, fallback)} onchange={(next) => set(key, next)} compact/>
            {#if Object.prototype.hasOwnProperty.call(override, key)}<span class="mt-1 block text-[.65rem] text-[var(--accent)]">Session override</span>{:else}<span class="muted mt-1 block text-[.65rem]">Inherited</span>{/if}
          </div>
        {/each}
      </div>
      {#if applicable.length > (common[section]?.length ?? 0)}<button onclick={() => advanced = !advanced} class="muted mt-5 flex items-center gap-1 text-xs font-semibold"><ChevronDown class={advanced ? 'rotate-180' : ''} size={14}/>{advanced ? 'Hide' : 'Show'} advanced settings</button>{/if}
    {/if}
  {/if}
  {#if message}<p class="mt-4 text-xs" class:text-red-500={message.includes('invalid') || message.includes('changed')}>{message}</p>{/if}
</section>

<style>.tool{display:flex;align-items:center;gap:.35rem;border:1px solid var(--line);border-radius:.65rem;padding:.5rem .65rem;font-size:.7rem;font-weight:700}.research-card{border:1px solid var(--line);border-radius:1rem;background:color-mix(in srgb,var(--paper-strong) 86%,transparent);padding:1rem}.research-card.enabled{border-color:color-mix(in srgb,var(--accent) 38%,var(--line));box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--accent) 7%,transparent)}</style>
