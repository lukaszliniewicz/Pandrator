<script lang="ts">
  import { AudioWaveform, CheckCircle2, Cpu, KeyRound, Mic2, ShieldCheck, Volume2, X } from '@lucide/svelte';
  import { appState } from './app-state.svelte';
  let { onclose }: { onclose: () => void } = $props();
  const ready = (key:string) => Boolean(appState.capabilities?.services?.[key] ?? appState.capabilities?.[key]?.available);
</script>

<div class="fixed inset-0 z-50 bg-black/35" role="presentation">
  <aside class="surface absolute inset-y-0 right-0 w-full max-w-xl overflow-y-auto p-7 sm:p-9" aria-label="Setup checklist">
    <header class="flex items-start justify-between gap-4"><div><div class="eyebrow">Setup checklist</div><h1 class="mt-2 text-3xl font-semibold">Prepare your studio</h1><p class="muted mt-2 text-sm">Configure only what your workflow needs. Pandrator never installs components from this screen.</p></div><button onclick={onclose} aria-label="Close setup checklist" class="rounded-xl border border-[var(--line)] p-2"><X size={18}/></button></header>
    <div class="mt-8 space-y-3">
      <a href="/providers" onclick={()=>appState.showSetupReturn('Choose and test an LLM provider, then return to continue setup.')} class="item"><KeyRound/><span><strong>LLM providers</strong><small>Correction, translation, TTS optimization, and document-cleaning agents.</small></span></a>
      <a href="/providers" onclick={()=>appState.showSetupReturn('Connect and test speech services, then return to continue setup.')} class="item"><Volume2/><span><strong>Speech services</strong><small>{Object.values(appState.capabilities?.services??{}).filter(Boolean).length} local components currently detected.</small></span></a>
      <a href="/voices" onclick={()=>appState.showSetupReturn('Add reference recordings and transcripts, then return to continue setup.')} class="item"><Mic2/><span><strong>Voice library</strong><small>Add, record, preview, transcribe, and organize reusable voice references.</small></span></a>
      <a href="/rvc" onclick={()=>appState.showSetupReturn('Add optional RVC model weights and indexes, then return to continue setup.')} class="item"><AudioWaveform/><span><strong>RVC speech conversion</strong><small>RVC {ready('rvc')?'ready':'not detected'} · manage paired .pth weights and .index retrieval data.</small></span></a>
      <a href="/training" onclick={()=>appState.showSetupReturn('Review optional XTTS fine-tuning, then return to continue setup.')} class="item"><Cpu/><span><strong>XTTS fine-tuning</strong><small>Prepare datasets and train reusable XTTS voices through recoverable jobs.</small></span></a>
      <a href="/settings" class="item"><ShieldCheck/><span><strong>Privacy and storage</strong><small>Review output location, retention, authentication, and API-cost implications.</small></span></a>
    </div>
    <div class="mt-8 rounded-2xl border border-[var(--line)] bg-[var(--accent-soft)] p-5"><div class="flex items-center gap-2 font-semibold"><CheckCircle2 size={18}/> Readiness summary</div><p class="muted mt-2 text-sm">CrispASR {ready('crispasr')?'is ready':'is not detected'}. FFmpeg {appState.capabilities?.ffmpeg?.available?'is ready':'is missing'}. Missing components stay disabled and can be installed later through the installer.</p></div>
  </aside>
</div>

<style>.item{display:flex;gap:.9rem;border:1px solid var(--line);border-radius:1rem;padding:1rem}.item:hover{border-color:var(--accent);background:var(--accent-soft)}.item :global(svg){flex:none;color:var(--accent)}.item strong,.item small{display:block}.item small{margin-top:.25rem;color:var(--muted);font-size:.75rem;line-height:1.45}</style>
