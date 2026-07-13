# Web parity audit

This register complements `pandrator.web.parity_registry`. “Implemented” means a user-facing path exists; it does not mean release-qualified until the listed evidence is present.

| User path | Current web surface | State | Remaining acceptance evidence |
|---|---|---:|---|
| First launch and setup | Home setup checklist, Providers & Services, Voices, Application Settings | Partial | First-run persistence, missing-component guidance, local/remote variants, and return-to-setup Playwright coverage |
| Outcome-specific creation | Home tiles and guided creation | Implemented | Each tile must begin with its preselected outcome; all SRT/media/document branches and later workflow promotion need browser tests |
| Full custom workspace | Guided creation shortcut and session outcome editor | Implemented | Verify no guided choice hides later stages or deletes existing artifacts |
| Reusable sources | Source Library and session Sources | Partial | Revision history, reference-aware trash/delete, external-reference policy, and doctor/repair actions |
| Asset inspection | Reusable artifact preview modal | Implemented | Text/subtitle, JSON, audio, video, image, PDF, very large file, missing file, and unsupported type component tests |
| Media to subtitles | Session workflow, CrispASR, subtitle composition | Partial | Whisper/Parakeet word timing, VAD combinations, diarization availability, correction/translation parent selection, and export fixtures |
| Existing SRT to voiceover | Guided creation, workflow cards, generation drawer | Partial | Direct, corrected, translated, and correction→translation paths with stale-descendant assertions |
| Subtitle review | Text & Subtitles comparison editor | Partial | Legacy temporal alignment, lineage merges/splits, timing conflicts, audio scrubbing, save conflict, and reviewed-revision invalidation |
| Audiobook generation | Session text/voice/output tabs and generation drawer | Partial | Long-document chapters, cover picker, metadata preview, M4B chapters/cover, pause/restart recovery, and output validation |
| Voice library | Voices | Partial | Chrome/Firefox permissions, device switching, Record/Stop/Play/Discard/Save, FFmpeg failure, STT cancellation, and transcript persistence |
| TTS generation review | Generation drawer | Partial | Tens-of-thousands virtualization, selected/marked/stale/failed/all operations, ETA, keyboard and continuous-playback tests |
| RVC speech-to-speech | RVC conversion route, generation drawer, session RVC settings | Partial | `.pth`/`.index` pairing, defaults, selected/marked/all conversion, take ancestry, failed conversion, and automatic-generation policy |
| Agentic source cleaning | Session Cleaning | Partial | Phase/action/diff acceptance, retry, cost display, representative EPUB/PDF fixtures, and no private reasoning exposure |
| PDF preprocessing | PDF editor | Partial | 1,000-page progressive test, left/right first-side mapping, mixed geometry/rotation, encrypted/malformed input, undo/redo provenance |
| Export | Output tab and export stage | Partial | Original/mixed/dub-only audio × none/source/translation/dual soft/burned subtitles, audio-only/SRT output, language metadata, M4B |
| Providers and costs | Providers & Services | Partial | Adapter-by-adapter schema visibility, model refresh/merge/delete, zero temperature, custom reasoning, cached-token payloads, authoritative-cost precedence |
| Remote deployment | Authenticated Flask/Waitress runtime | Partial | Caddy smoke test, CSRF/proxy/host tests, browser recording secure-context behavior, download-only local actions, threat-model gate |
| Installer/launcher | Qt supervisor and headless CLI | Partial | Windows/Fedora lifecycle matrix, browser reopening, explicit LAN host plus loopback health, signed update rollback, clean uninstall |

## Immediate release blockers

1. Complete the export matrix and audiobook cover/chapter workflow.
2. Qualify subtitle review lineage and generation recovery against migrated Qt and web-preview fixtures.
3. Finish source revision/deletion/repair UX.
4. Run real CrispASR, voice recording/playback, TTS, and RVC smoke tests on supported hardware.
5. Close every partial parity-registry entry with automated evidence or an explicit documented removal rationale.
