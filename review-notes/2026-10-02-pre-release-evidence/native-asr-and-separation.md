# Native ASR and separation evidence

**Evidence date:** 2026-10-02. **Phase:** pre-release source-tree acceptance. This note summarizes the bounded detector, transcription, and vocal-separation runs made against the current in-tree Pandrator code. It is not a release-artifact claim or a product go/no-go decision; the parent orchestrator retains that judgment. This documentation pass performed no inference, service operation, or product edit.

The runs used CrispASR 0.8.40 with CPU and Vulkan backends and audio.cpp 0.9.0 (`git 795c45fb`, Release, GCC 13.3.0, Linux x86_64; CPU/Vulkan build). The acceptance harness imported the current repository source. Test inputs were copied into task scratch and verified against their manifests; the recorded source-integrity checks remained true. No transcript text, source-audio path, or fixture voice identity is reproduced here. Hashes below cover model or generated/evidence artifacts, not source-media bytes.

## Language detector

Whisper.cpp Tiny was verified from `ggerganov/whisper.cpp` revision `5359861c739e955e79d9a303bcbc70fb988958b1`, SHA-256 `be07e048e1e599ad46341c8d2a135645097a538221678b7acdd1b1919c6e1b21`, 77,691,713 bytes. All native detector trials used CPU, 16 kHz mono signed 16-bit PCM, and a 0.70 acceptance threshold.

| Input ID | Duration | Parsed language | Confidence | Threshold result | Elapsed | Result |
|---|---:|---|---:|---|---:|---|
| EN-01 | 12.635 s | en | 0.996003 | accepted | 1.611 s | pass |
| DE-01 | 14.480 s | de | 0.997086 | accepted | 1.831 s | pass |
| JA-01 | 15.000 s | ja | 0.983825 | accepted | 1.583 s | pass |
| SILENCE | 15.000 s | en | 0.459397 | not accepted | 1.632 s | synthetic negative control; pass |
| TONE_440HZ_MINUS40DBFS | 15.000 s | en | 0.465006 | not accepted | 1.502 s | synthetic negative control; pass |

The three spoken candidates met the expected language and threshold checks. Neither synthetic negative control was accepted, and the detector summary recorded no high-confidence negative-control anomaly. The actual native-command cancellation check passed in 0.318 s: the owned child exited, its temporary directory was removed, and no fallback ran.

## Timed transcription

Each ASR input was normalized to 16 kHz mono signed 16-bit PCM. The runs used the shared transcription pipeline with the no-fetch pre-spawn guard. Native commands completed and outputs were present and hashed in all five execution cases. Parakeet used the verified existing cache entry; its recorded source is `cstr/parakeet-tdt-0.6b-v3-GGUF`, **unversioned main / legacy cache source**, SHA-256 `300de963db10e991a8c3c1674000245546f2e99d396f860aecbde2dd0534e43f`, 674,342,336 bytes. Qwen used `cstr/qwen3-asr-0.6b-GGUF` revision `f5814fb07a955e84b4474133002cd2bbc747c4b9`, SHA-256 `f547589d5ca582e093b2d3312ad9ff13b609b43d413f972c0e92b823dde70a00`, 1,006,809,760 bytes. Its forced aligner was `cstr/qwen3-forced-aligner-0.6b-GGUF` revision `d75b1dba5954f9ce25a7432ae38dc24813dbbdac`, SHA-256 `539df5dd0fe1721e378ac13bfac9a26b1260dafb62d892c518c1f21244762636`, 985,594,624 bytes.

| Case | Backend | Resolved route | Input duration | Elapsed | CER | WER | Word anchors | Strict case |
|---|---|---|---:|---:|---:|---:|---:|---|
| EN-01 | CPU | Parakeet, en | 12.635 s | 15.636 s | 0.0000 | 0.0000 | 38 valid / 0 invalid | pass |
| EN-01 | Vulkan | Parakeet, en | 12.635 s | 9.773 s | 0.0000 | 0.0000 | 38 valid / 0 invalid | pass |
| DE-01 | CPU | Parakeet, de | 14.480 s | 13.069 s | 0.0120 | 0.0313 | 32 valid / 0 invalid | pass |
| JA-01 | CPU | Qwen3 + forced aligner, ja | 16.560 s | 28.934 s | 0.0441 | 1.0000* | 63 valid / 5 invalid | fail: zero-width anchors |
| JA-01 | Vulkan | Qwen3 + forced aligner, ja | 16.560 s | 21.605 s | 0.0441 | 1.0000* | 63 valid / 5 invalid | fail: zero-width anchors |

All five ASR cases passed the native-command no-fetch guard and retained unchanged source checks. Both Japanese runs resolved the expected route, completed inference and alignment, and produced output files, but the strict all-words-positive-duration condition failed on five native zero-width word spans. Thus the inference acceptance components ran; **the strict all-word-positive harness did not pass**. Japanese WER is a coarse whitespace-tokenization metric because Japanese does not generally delimit words with spaces; CER is the more interpretable score in this sample. Reference text is not included.

### Japanese timing anchors

The five zero-based word indices and neighboring raw timings were identical on CPU and Vulkan. Values are `[start_ms,end_ms]`; the composer’s working interval is shown separately from native word evidence.

| Word index | Previous / native / next timings (ms) | Composer working span (ms) | Owning SRT cue (ms) |
|---:|---|---|---|
| 4 | `[1120,1520]` / `[1520,1520]` / `[2000,2400]` | `[1520,1540]` | `[400,5120]` |
| 26 | `[7680,7840]` / `[7840,7840]` / `[7840,8080]` | `[7840,7860]` | `[5760,9120]` |
| 47 | `[11840,12160]` / `[12160,12160]` / `[12160,12320]` | `[12160,12180]` | `[9760,12660]` |
| 50 | `[12320,12640]` / `[12640,12640]` / `[13120,13360]` | `[12640,12660]` | `[9760,12660]` |
| 57 | `[14240,14640]` / `[14640,14640]` / `[14640,14800]` | `[14640,14660]` | `[13120,16080]` |

The emitted SRT has four cues: `[400,5120]`, `[5760,9120]`, `[9760,12660]`, and `[13120,16080]` ms. All four end after they start; durations are 4,720, 3,360, 2,900, and 2,960 ms. Saved SRT intervals match in-memory composition for both backends. Native validation rejects `end < start`, so equality is accepted (`pandrator/logic/dubbing/crispasr.py:696-761`). The Qwen transcript adapter enables zero spans (`pandrator/logic/dubbing/transcript_normalization.py:193-202,337-350`), preserving them in normalized word evidence. Subtitle composition then applies a 20 ms minimum to its in-memory word spans (`pandrator/logic/dubbing/subtitle_finalization.py:688-727`); that adjustment does not rewrite the native word timestamps. `qwen_asr.transcribe` delegates the timed result to `crispasr.transcribe` (`pandrator/logic/dubbing/qwen_asr.py:600-692`), and the transcription orchestrator composes SRT from the saved word JSON (`pandrator/logic/dubbing/transcription.py:580-588`). Its final SRT post-processing only renumbers cues (`transcription.py:611-628`). `SpeechSpan` in the separate caption-alignment path rejects `end <= start` (`caption_alignment.py:33-39`); that path was not used for these ASR-to-SRT runs.

## HTDemucs separation

The selected native asset was `htdemucs_q8_0`, family and CLI family `htdemucs`, using the four-stem HTDemucs model while returning the vocals stem. Public pin: `repoaudio-cpp/audio.cpp-gguf`, revision `351dbab8d8534675ee29440bb402e348b09e55e2`, `HTDemucs-GGUF/htdemucs-q8_0.gguf`, 61,940,768 bytes. The existing scratch cache file independently hashed to the expected SHA-256 `b0f532ac6e5f373aeb11fa0df73253251e133832d9c8b9942dc58f50bc5b4388`.

The short test used the same 12.635 s mixed-audio input for CPU and Vulkan. Successful outputs were 44.1 kHz stereo signed 16-bit PCM with matching duration, finite samples, and no clipped samples. Repeated output hashes matched within each backend.

| Input length / backend | Runs | Runner elapsed | GNU wall | Peak native RSS | Peak native VRAM | Output RMS | Output SHA-256 |
|---|---|---:|---:|---:|---:|---:|---|
| Short / CPU | r1, r2 | 34.522 s; 31.887 s | 34.95 s; 32.24 s | 965,444; 966,560 KiB | n/a | 0.017394 | `bcf0512157057ef07c72c29d00c3778f8affda9a4484e1521d80308e6586c775` |
| Short / Vulkan | r1, r2 | 3.812 s; 2.789 s | 4.20 s; 3.18 s | 379,716; 333,316 KiB | 445 MiB | 0.111432 | `8167b5d46055e2850ca8c788ed1821e88b37df3c345e2097467e1d52d91968fa` |
| 300 s repeated test / Vulkan | r1, r2 | 41.604 s; 39.679 s | 48.72 s; 47.43 s | 1,377,712; 1,377,944 KiB (~1.38 GB) | 445 MiB | 0.111672 | `3d13b1d2a27c2a1578dd27e5f537234e79a8f61f49a13c9471e41f361609566a` |

For the short input, Vulkan output RMS was **6.406×** the CPU output RMS; repeats were byte-identical within each backend. This is a material amplitude discrepancy, not evidence of better separation. The 300 s case was a repeated synthetic music/mixed-audio test clip, not a listening evaluation. No ASR or listening/content-quality assessment was performed. Finite, unclipped outputs and preserved duration establish output-format/runtime behavior only; they do not establish speech-separation quality.

The CPU long-duration comparators are recorded separately from pass/fail quality evidence: one reached the 600 s timeout (600.411 s elapsed, no output); the other was canceled at 137.759 s (no output). A short CPU cancellation check stopped the native process in 2.627 s and left no temporary directory. These bounded timeout/cancellation comparisons are not counted as repeatable model failures, and they do not establish successful 300 s CPU performance. The Vulkan long runs completed, but cold model-load time and model-inference time were not separately recorded (`null` in run summaries); elapsed wall time includes startup/load and processing.

## Checksum register and evidence locations

All paths below are evidence/result metadata relative to `/tmp/pandrator-native-acceptance-20261002/`, not source-media paths. Raw logs and word JSON are not copied into this note. Input-media hashes are omitted; the run metadata records unchanged-source checks. SHA-256 values are full-length.

**Detector metadata**

- `detector/trial_results.json`: `3aee6baabb82379fa3739f9befba3d745be93af80429d343eace79e4b64fe567`
- `detector/final_summary.json`: `e0953e95d128d3ff38b45beea6c4f0af6f7116a992a89d45fc23023574730e78`
- `detector/cancellation_result.json`: `4d83cf839bd2d2853d40e4947afd14eead98d8ab42f6dac65a61b4852bfd4483`

**ASR metadata and generated outputs**

- `asr/final_summary.json`: `67d31b434eef6c4153481429108338535108ae0d6907528c7236794953e3a584`
- `asr/japanese_zero_duration_timing_followup.json`: `39e0611376c62e46aa1a9f8ddd6e8322764a4fcc33d818ca657327bd8d5a78ff`

| Case | `result.json` SHA-256 | SRT SHA-256 | Word JSON SHA-256 |
|---|---|---|---|
| DE-01 CPU | `ebde53a6fed30c9ef008302a174d9d7bf107340e01e792dfa60abb40029f70de` | `ab9dc64c86c7f203c01968e93e1548735752b307df8f381ee0706ac2f0c05247` | `ec862c6b4e9f383aaaa481672f75b770a64ef4a0097d4205255d8f79155e745b` |
| EN-01 CPU | `bae0f6f77ff135e45ad5a74bb095fc3659898a7d6e4a8a237fa7b1b38513ddd4` | `91c192359c277c90a33fa126a7a631449e86349d6c49c7dad4ef74f4345197de` | `4b539cd501dd8f5bad7d71c91a492225723b0bcd599fb56f655d748283f720cd` |
| EN-01 Vulkan | `dd8851cb6e1b7cbcfaa130fb4aad5afe94293e34b65d8910da1599da97bd6236` | `91c192359c277c90a33fa126a7a631449e86349d6c49c7dad4ef74f4345197de` | `ae3c783af2365c2d02a0fbb0c59543745723545f46f1a7b3b2a62110459341d8` |
| JA-01 CPU | `60b2e0d83bc33c87c57572f175f5f687e179b717d2ed2ed3a77cf155fbd91e9e` | `1d5ef6839a7ee5561599c2fe5970634998fce98a474cf9df85bf33ee279a05f4` | `b663f8546b2282021504122eb6a47e7815592d99076323a2102a9f8dd54eb0a7` |
| JA-01 Vulkan | `156af68cb8f46fa175fffdea7a837a39fcb3299845881df81b9fcbafdec8c336` | `1d5ef6839a7ee5561599c2fe5970634998fce98a474cf9df85bf33ee279a05f4` | `1c852b3609873ac6c89848209a5990eb5aff59a6737fc49b4da362977175a7ca` |

**HTDemucs runs**

| Run summary relative path | SHA-256 |
|---|---|
| `separation/runs_native/htdemucs_cpu_short_r1/run-summary.json` | `f6d817fb87e5205bb0e52b7ff2046485e8804d3964c054085428d3e53b302c28` |
| `separation/runs_native/htdemucs_cpu_short_r2/run-summary.json` | `07f0ea22a65373117165780a8f1264330377867a9836351dc4f17b673f8d5516` |
| `separation/runs_native/htdemucs_vulkan_short_r1/run-summary.json` | `36d7e8d95abd1b5a487fa814866840c651bdc4faffea882f1ccb5983eca8eb79` |
| `separation/runs_native/htdemucs_vulkan_short_r2/run-summary.json` | `f9364a952cff1f9a5a2331b2d33bfcdd66b1960271bf549d089c72473d480c81` |
| `separation/runs_native/htdemucs_vulkan_long_r1/run-summary.json` | `42ecd1b11e0274004c44487475869953efdf91ce2eb8e96ff6ba528aea66d291` |
| `separation/runs_native/htdemucs_vulkan_long_r2/run-summary.json` | `9900104818c6a7c430e44e43024fe38c8019f6357dd4e314a97fb49294a88693` |
| `separation/runs_native/htdemucs_cpu_long_r1/run-summary.json` | `adef1f7e90cefd1a8fe1254ca9be6f26bf4ffd2b5d0071a0dc38eceb5b8228f6` |
| `separation/runs_native/htdemucs_cpu_long_r2/run-summary.json` | `d097ab20946242aee2fa96b78b130f5e8cda25602b8563abc0af4d37b41cd857` |
| `separation/runs_native/htdemucs_cpu_short_r99_cancel/run-summary.json` | `b1818aeebaa88d4d4f1617833ea5c496530f757dea60505ea22b701527007fc4` |
| `separation/experiment-summary.json` | `a80c11f7974e521e61170e472d3617aa29c3fda88184e4b0b4abfd76eb2b7f01` |
