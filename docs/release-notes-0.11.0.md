[![Download for Windows (.exe)](https://img.shields.io/badge/Download_for_Windows-.exe-2563eb?style=for-the-badge)](https://github.com/lukaszliniewicz/Pandrator/releases/download/v.0.11.0/PandratorManager-0.9.28-windows-x86_64.exe) [![Download for Linux (.AppImage)](https://img.shields.io/badge/Download_for_Linux-.AppImage-168572?style=for-the-badge)](https://github.com/lukaszliniewicz/Pandrator/releases/download/v.0.11.0/PandratorManager-0.9.28-x86_64.AppImage)

**Pandrator 0.11.0 expands language support, improves transcription routing, and completes multilingual export collection.**

Download the Windows `.exe` or Linux `.AppImage` above to install or update through Pandrator Manager. GitHub's **Source code** archives are for developers.

Includes **Pandrator 0.11.0**, **Manager 0.9.28**, and **MCP 0.6.0**. Managed audio.cpp uses **0.9.0**; automatic language detection uses pinned **CrispASR 0.8.40**.

- **Language choices follow the selected model.** Search 745 language entries, retain regional/script tags and custom values, and see exact, partially documented or unverified coverage. TTS, recognition, alignment and voice-design support stay separate. Silero pack coverage and provider aliases are tracked explicitly.
- **Automatic transcription prefers Parakeet.** It resolves the language first, selects Qwen when recognition and required timing are supported, and uses Whisper as a supported fallback. Existing explicit engine choices remain selected. TXT-only Qwen requests can use recognition languages without inventing subtitle timestamps.
- **Optional Demucs vocal isolation.** Four-stem Q8 separation is available in transcription with verified managed weights and cancellation; it remains off by default. The approximately 62 MB model is smaller than the existing RoFormer options. Performance and speech quality depend on the input and backend.
- **Subtitle review protects edits.** Spoken-passage and display-timing controls expose their ownership and verified boundaries. Delete/Undo and Save/Discard/Cancel preserve drafts after validation failures; speaker edits survive immutable saves. Unsupported video can use a managed compatible preview or explicit audio-only playback.
- **Multilingual projects show readiness.** Pinned source details and separate translation, review, voice, generation and export status explain the next action. Preview selected-language translation, generation or export; recover durable progress, preserve completed children and retry eligible failures.
- **Collect finished exports.** Verified manifests and optional ZIPs use stable project/language/version filenames and artifact hashes. Subtitle exports do not require generated audio. Publication receipts recover completed copies after worker interruption; incomplete collections stay incomplete.
- **Release repairs.** Settings saves, independent MCP startup, strict single-voice routing, canonical passage identity, unfinished-upload deletion and Python test collection are repaired. Language incompatibility and supplied speech-plan conflicts are checked before generation jobs start.

Existing sessions and generation history are retained.

Qwen may emit point-like raw word timestamps even when composed display subtitles have valid spans. Acoustic quality and word-timing precision vary by language and model.
