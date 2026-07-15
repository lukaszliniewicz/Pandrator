<script lang="ts">
  import { ChevronDown, ExternalLink, RotateCcw, Save } from '@lucide/svelte';
  import { api } from './api';
  import SettingField from './SettingField.svelte';

  let { sessionId, section, title, description = '' }: { sessionId: string; section: string; title: string; description?: string } = $props();
  let payload = $state<any>(null);
  let override = $state<Record<string, any>>({});
  let advanced = $state(false);
  let saving = $state(false);
  let message = $state('');
  const common: Record<string, string[]> = { text: ['enable_sentence_splitting', 'max_sentence_length', 'enable_sentence_appending', 'enable_nemo_normalization', 'normalize_all_caps', 'llm_tts_document_optimization', 'llm_tts_optimization', 'llm_tts_document_batch_size', 'llm_tts_batch_size'], stt: ['stt_engine', 'stt_compute_backend', 'stt_compute_device', 'stt_language', 'whisper_prompt', 'crispasr_vad_enabled', 'crispasr_vad_threshold', 'crispasr_vad_min_speech_ms', 'crispasr_vad_min_silence_ms', 'crispasr_vad_max_speech_seconds', 'crispasr_vad_speech_pad_ms', 'diarization_enabled'], subtitles: ['max_lines', 'max_chars_per_line', 'max_cps', 'min_duration_ms', 'max_duration_ms', 'min_gap_ms', 'phrase_gap_ms', 'boundary_correction_enabled', 'merge_threshold_ms'], correction: ['enabled', 'model_name', 'instructions', 'preserve_timing', 'max_subtitles_per_call', 'context_before', 'context_after', 'request_timeout_seconds'], translation: ['enabled', 'backend', 'source_language', 'target_language', 'professional_cleanup', 'model_name', 'instructions', 'glossary', 'glossary_enabled', 'context', 'max_subtitles_per_call', 'max_line_length', 'no_remove_subtitles', 'request_timeout_seconds'], tts: ['service', 'model', 'voice', 'language', 'speed', 'max_attempts'], audio: ['sentence_silence_ms', 'paragraph_silence_ms', 'fade_enabled', 'fade_in_ms', 'fade_out_ms', 'synchronization_delay_ms', 'synchronization_speed'], rvc: ['enabled', 'model', 'pitch', 'f0_method', 'filter_radius', 'index_rate', 'volume_envelope', 'protect'], source_cleaning: ['agentic', 'max_iterations', 'pdf_ocr_mode', 'pdf_ocr_language', 'pdf_ocr_dpi', 'pdf_remove_toc', 'pdf_remove_repeated_marginals', 'request_timeout_seconds'], output: ['format', 'bitrate', 'export_mode', 'audio_mode', 'subtitle_mode', 'subtitle_selection', 'subtitle_format', 'burn_video_encoder', 'burn_video_quality', 'burn_video_speed', 'burn_audio_codec', 'burn_audio_bitrate', 'title', 'artist', 'album', 'genre', 'language'] };
  const entries = $derived(Object.entries(payload?.effective ?? {}).sort(([left], [right]) => { const order = common[section] ?? []; const li = order.indexOf(left), ri = order.indexOf(right); return (li < 0 ? 999 : li) - (ri < 0 ? 999 : ri) || left.localeCompare(right); }));
  const providerSetting = (key: string) => key === 'provider_configs' || key === 'use_external_server' || key === 'external_server_url' || key === 'openai_audio_endpoint' || key.endsWith('_base_url') || key.endsWith('_api_key');
  const sectionName = (value: string) => ({ tts: 'TTS', stt: 'STT', rvc: 'RVC' } as Record<string, string>)[value] ?? value.replaceAll('_', ' ');
  const applicable = $derived(entries.filter(([key]) => { if (section !== 'tts') return true; if (providerSetting(key)) return false; const service = String(value('service', payload?.effective?.service ?? '')).toLowerCase(); if (key.startsWith('voxcpm_')) return service.includes('voxcpm'); if (key.startsWith('fishs2_')) return service.includes('fish'); if (key.startsWith('voxtral_')) return service.includes('voxtral'); if (key.startsWith('silero_')) return service.includes('silero'); if (key.startsWith('chatterbox_')) return service.includes('chatterbox'); if (key.startsWith('xtts_') || ['temperature', 'length_penalty', 'repetition_penalty', 'top_k', 'top_p', 'do_sample', 'num_beams', 'enable_text_splitting', 'stream_chunk_size', 'gpt_cond_len', 'gpt_cond_chunk_len', 'max_ref_len', 'sound_norm_refs', 'overlap_wav_len'].includes(key)) return service.includes('xtts'); if (key.startsWith('openai_audio_')) return service.includes('openai') || service.includes('gemini') || service.includes('custom'); return true; }));
  const visible = $derived(applicable.filter(([key]) => advanced || (common[section] ?? []).includes(key)));
  const deterministicText = $derived(applicable.filter(([key]) => !key.startsWith('llm_')).filter(([key]) => advanced || ['enable_sentence_splitting','max_sentence_length','enable_sentence_appending','enable_nemo_normalization','normalize_all_caps'].includes(key)));
  const llmText = $derived(applicable.filter(([key]) => key.startsWith('llm_') || ['tts_optimization_model','combined_prompt','first_prompt','second_prompt','third_prompt'].includes(key)).filter(([key]) => {
    if (['llm_tts_optimization','llm_tts_document_optimization'].includes(key)) return true;
    if (!Boolean(value('llm_tts_optimization', payload?.effective?.llm_tts_optimization)) && !Boolean(value('llm_tts_document_optimization', payload?.effective?.llm_tts_document_optimization))) return false;
    const divided=Boolean(value('llm_multi_stage', payload?.effective?.llm_multi_stage));
    if (['first_prompt','second_prompt','third_prompt'].includes(key)) return divided;
    if (key === 'combined_prompt') return !divided;
    return true;
  }));
  const value = (key: string, fallback: any) => Object.prototype.hasOwnProperty.call(override, key) ? override[key] : fallback;
  const set = (key: string, next: any) => override = { ...override, [key]: next };

  async function load() { payload = await api(`/sessions/${sessionId}/settings/${section}`); override = { ...(payload.override ?? {}) }; }
  async function save() { saving = true; message = ''; try { if (section === 'tts') override = Object.fromEntries(Object.entries(override).filter(([key]) => !providerSetting(key))); payload = await api(`/sessions/${sessionId}/settings/${section}`, { method: 'PUT', headers: { 'If-Match': `"${payload.revision}"` }, body: JSON.stringify({ value: override }) }); override = { ...payload.override }; message = 'Saved for this session.'; } catch (caught) { message = caught instanceof Error ? caught.message : String(caught); } finally { saving = false; } }
  async function reset() { saving = true; message = ''; try { payload = await api(`/sessions/${sessionId}/settings/${section}`, { method: 'PUT', headers: { 'If-Match': `"${payload.revision}"` }, body: JSON.stringify({ value: {} }) }); override = {}; message = 'Reverted to application defaults.'; } catch (caught) { message = caught instanceof Error ? caught.message : String(caught); } finally { saving = false; } }
  async function saveAsDefaults() {
    saving = true; message = '';
    try {
      const promoted = section === 'tts' ? Object.fromEntries(Object.entries(override).filter(([key]) => !providerSetting(key))) : override;
      const defaults = await api<any>(`/defaults/${section}`);
      await api(`/settings/defaults.${section}`, { method: 'PUT', headers: { 'If-Match': `"${defaults.revision}"` }, body: JSON.stringify({ value: { ...(defaults.value ?? {}), ...promoted } }) });
      payload = await api(`/sessions/${sessionId}/settings/${section}`, { method: 'PUT', headers: { 'If-Match': `"${payload.revision}"` }, body: JSON.stringify({ value: Object.fromEntries(Object.entries(override).filter(([key]) => !Object.prototype.hasOwnProperty.call(promoted, key))) }) });
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
    <div class="flex flex-wrap gap-2">{#if section === 'tts'}<a href="/providers?tab=tts" class="tool"><ExternalLink size={14}/> TTS services</a>{/if}<button onclick={reset} disabled={saving || !Object.keys(override).length} class="tool"><RotateCcw size={14}/> Revert to defaults</button><button onclick={saveAsDefaults} disabled={saving || !Object.keys(override).length} class="tool"><Save size={14}/> Save as defaults</button><button onclick={save} disabled={saving} class="tool bg-[var(--accent)] text-white"><Save size={14}/> {saving ? 'Saving…' : 'Save'}</button></div>
  </div>
  {#if payload}
    {#if section==='text'}
      <div class="mt-5 grid gap-5 xl:grid-cols-2">
        <section class="rounded-2xl border border-[var(--line)] p-4"><div class="text-sm font-semibold">Segmentation and deterministic processing</div><p class="muted mt-1 text-xs leading-relaxed">Creates generation units, paragraph boundaries, and predictable text normalization without an LLM or provider cost.</p><div class="mt-4 grid gap-4 sm:grid-cols-2">{#each deterministicText as [key,fallback]}<div><SettingField {section} keyName={key} value={value(key,fallback)} onchange={(next)=>set(key,next)} compact/>{#if Object.prototype.hasOwnProperty.call(override,key)}<span class="mt-1 block text-[.65rem] text-[var(--accent)]">Session override</span>{:else}<span class="muted mt-1 block text-[.65rem]">Inherited</span>{/if}</div>{/each}</div></section>
        <section class="rounded-2xl border border-[var(--line)] bg-[var(--accent-soft)] p-4"><div class="text-sm font-semibold">Optional LLM speech optimization</div><p class="muted mt-1 text-xs leading-relaxed">Runs per generation segment immediately before synthesis. Local models can run without API charges; remote models may incur cost. Original segment text is preserved.</p><div class="mt-4 grid gap-4">{#each llmText as [key,fallback]}<div><SettingField {section} keyName={key} value={value(key,fallback)} onchange={(next)=>set(key,next)} compact/><span class="mt-1 block text-[.65rem] text-[var(--accent)]" class:muted={!Object.prototype.hasOwnProperty.call(override,key)}>{Object.prototype.hasOwnProperty.call(override,key)?'Session override':'Inherited'}</span></div>{/each}</div></section>
      </div>
      <button onclick={() => advanced = !advanced} class="muted mt-5 flex items-center gap-1 text-xs font-semibold"><ChevronDown class={advanced ? 'rotate-180' : ''} size={14}/>{advanced ? 'Hide' : 'Show'} advanced deterministic settings</button>
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

<style>.tool{display:flex;align-items:center;gap:.35rem;border:1px solid var(--line);border-radius:.65rem;padding:.5rem .65rem;font-size:.7rem;font-weight:700}</style>
