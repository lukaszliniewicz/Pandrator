# Parakeet ONNX ASR Spike

Date: 2026-07-09

## Scope

Test `onnx-asr[cpu,hub]` with the non-quantized NVIDIA Parakeet TDT 0.6B V3 ONNX alias on CPU, using a real meeting recording and Silero VAD with `max_speech_duration_s=60`.

The initial spike was kept out of the installer and out of Pandrator runtime dependencies. After the spike, ONNX Parakeet was integrated as an optional STT backend with an isolated Pixi environment.

## Scratch Environment

Scratch directory:

```text
C:\Users\user\Downloads\Pandrator_dev\parakeet_onnx_spike
```

Setup used:

```powershell
pixi init C:\Users\user\Downloads\Pandrator_dev\parakeet_onnx_spike
pixi add python=3.11 pip ffmpeg
pixi run python -m pip install "onnx-asr[cpu,hub]"
```

Installed package versions observed:

- `onnx-asr 0.11.0`
- `onnxruntime 1.27.0`
- `numpy 2.4.6`
- ONNX Runtime providers: `AzureExecutionProvider`, `CPUExecutionProvider`

Windows cache setting needed:

```powershell
$env:HF_HOME = "C:\Users\user\Downloads\Pandrator_dev\parakeet_onnx_spike\hf_home"
$env:HF_HUB_DISABLE_SYMLINKS = "1"
$env:HF_HUB_DISABLE_SYMLINKS_WARNING = "1"
```

Without `HF_HUB_DISABLE_SYMLINKS=1`, Hugging Face cache creation failed on Windows with `WinError 1314` because the non-admin process could not create symlinks.

Approximate disk use after the FP32 model download:

- Pixi env: 802 MB
- Hugging Face cache: 2.4 GB
- Largest model files:
  - `encoder-model.onnx.data`: 2322.6 MB
  - `decoder_joint-model.onnx`: 69.2 MB
  - `encoder-model.onnx`: 39.8 MB

## Test Audio

Source recording:

```text
C:\Users\user\Downloads\GMT20260621-143017_Recording.m4a
```

Source duration: 3959.64 seconds, about 66 minutes.

Extracted 15-minute test WAV:

```powershell
pixi run ffmpeg -hide_banner -y `
  -i "C:\Users\user\Downloads\GMT20260621-143017_Recording.m4a" `
  -t 900 -ac 1 -ar 16000 -c:a pcm_s16le `
  .\samples\meeting_15m_16k_mono.wav
```

Extracted file:

```text
C:\Users\user\Downloads\Pandrator_dev\parakeet_onnx_spike\samples\meeting_15m_16k_mono.wav
```

Size: 28.8 MB.

## API Shape

Non-quantized CPU load:

```python
import onnx_asr

vad = onnx_asr.load_vad("silero", providers=["CPUExecutionProvider"])

model = (
    onnx_asr.load_model(
        "nemo-parakeet-tdt-0.6b-v3",
        providers=["CPUExecutionProvider"],
    )
    .with_vad(vad, max_speech_duration_s=60)
    .with_timestamps()
)

segments = list(model.recognize("meeting_15m_16k_mono.wav"))
```

Important observations:

- With VAD enabled, `recognize()` returns a generator. It must be consumed to run inference.
- Segment objects expose `start`, `end`, `text`, `tokens`, `timestamps`, and `logprobs`.
- `timestamps` are token timestamps relative to the segment start, not absolute timeline timestamps.
- `tokens` are sub-word tokens, not word tokens.
- A few VAD segments can return empty text and should be filtered before SRT generation.
- `max_speech_duration_s=60` is the correct keyword for the requested 60s max speech setting.

## Results

Short 60-second sanity run:

- Segments: 17
- Recognition time after model load: 19.5 seconds
- RTF: 0.325
- Speed: 3.08x real time

Full 15-minute run:

- Audio duration: 900.0 seconds
- Model/VAD load time from local cache: 9.45 seconds
- Recognition time: 428.3 seconds
- RTF: 0.476
- Speed: 2.1x real time
- Segments total: 236
- Non-empty segments: 231
- Empty segments: 5
- Segment duration range: 0.252s to 15.452s
- Average segment duration: 2.09s
- Max text length in one segment: 231 chars
- Average non-empty text length: 30.7 chars
- Max token count in one segment: 85

Output artifacts written in the scratch directory:

```text
outputs\meeting_15m_parakeet_fp32_vad60.json
outputs\meeting_15m_parakeet_fp32_vad60.srt
outputs\meeting_15m_parakeet_fp32_vad60.txt
```

The generated English transcript was broadly coherent for conversational meeting audio, with punctuation and disfluencies preserved. There were still normal ASR issues: repeated words, occasional odd wording, no speaker diarization, and segment text sometimes long enough to need Pandrator's existing correction/equalization stages.

## Short VAD Comparison

A second comparison used a 2-minute excerpt from `05:00-07:00` of the same meeting file.

Important behavior:

- Parakeet adds punctuation inside recognized segment text.
- Segment start/end times come from Silero VAD boundaries, not from sentence-level punctuation or semantic segmentation.
- Token timestamps are sub-word/token timestamps and should not be treated as WhisperX-style word alignment.
- `nemo-parakeet-tdt-0.6b-v3` is the multilingual v3 model, not the English-only v2 model. NVIDIA lists 25 supported languages: Bulgarian, Croatian, Czech, Danish, Dutch, English, Estonian, Finnish, French, German, Greek, Hungarian, Italian, Latvian, Lithuanian, Maltese, Polish, Portuguese, Romanian, Slovak, Slovenian, Spanish, Swedish, Russian, and Ukrainian.

Observed comparison:

| VAD settings | Non-empty segments | Average duration | Max duration | Punctuated segments |
| --- | ---: | ---: | ---: | ---: |
| `max_speech_duration_s=60`, Silero defaults | 14 | 2.0s | 4.8s | 13/14 |
| `max_speech_duration_s=15`, Silero defaults | 14 | 2.0s | 4.8s | 13/14 |
| `threshold=0.25`, `min_silence_duration_ms=1000`, `max_speech_duration_s=15` | 8 | 4.0s | 10.7s | 7/8 |
| `threshold=0.5`, `min_silence_duration_ms=500`, `max_speech_duration_s=15` | 9 | 3.3s | 8.5s | 9/9 |

Default recommendation for the first integration: keep Silero's default `threshold=0.5`, `min_silence_duration_ms=100`, `min_speech_duration_ms=250`, and `speech_pad_ms=30`, but cap `max_speech_duration_s` at 15. Expose the VAD controls in the advanced UI so longer or noisier media can be tuned per run.

## Integration Implications

Implemented shape:

1. `pandrator.logic.dubbing.stt_backends` owns backend identifiers and normalization.
2. `pandrator.logic.dubbing.parakeet_onnx` owns Parakeet loading, VAD options, JSON serialization, SRT writing, and Pixi subprocess execution.
3. `pandrator.logic.dubbing.transcription.transcribe_video_file()` extracts one normalized WAV, then routes to WhisperX or ONNX Parakeet before shared SRT post-processing.
4. Parakeet writes:
   - `video_name.srt`
   - `video_name_parakeet.json`
   - optional `video_name_parakeet.txt`
5. Parakeet JSON stores segment `start`, `end`, `text`, `tokens`, relative `timestamps`, absolute token timestamps, `logprobs`, model id, provider, quantization, and VAD settings.
6. Empty VAD segments are filtered before SRT generation.
7. The primary transcription UI exposes the installed STT backends and backend-specific language list. The Whisper model selector is shown only for WhisperX. The advanced transcription modal dynamically shows either WhisperX settings or the Parakeet group. Parakeet controls expose:
   - Parakeet model id
   - quantization: FP32 / int8
   - VAD enabled
   - VAD max speech duration, default 15s
   - VAD threshold
   - VAD negative threshold
   - VAD minimum silence duration
   - VAD minimum speech duration
   - VAD speech padding
   - VAD batch size
   - optional TXT output
8. Installer support is an optional `parakeet_onnx` component with a dedicated `envs/parakeet_onnx_installer` Pixi environment. The app uses that environment through `pixi run --manifest-path` when it exists, keeping `onnx-asr` isolated from the core Pandrator runtime.
9. The UI detects installed STT backends from optional Pixi manifests, importable modules, or CLI availability, and filters transcription languages by backend. WhisperX keeps the broad Whisper language list; ONNX Parakeet v3 exposes its 25-language set and records the selected language in JSON while leaving recognition in auto-detect mode.

Future segmentation note:

- The first baseline uses Parakeet's VAD-provided segments directly.
- Later STT work may use `wtpsplit-lite` to build more controlled subtitle segments from Parakeet text and token JSON. This should be handled as a separate segmentation layer because Parakeet token timings are sub-word timings, not word-level alignment.

Implementation cautions:

- The FP32 model is large enough that it should be an optional component, not part of the base install.
- `onnx-asr` currently pulled `numpy 2.4.6`; Pandrator's focused Pixi test environment uses `numpy 1.26.4`, so this dependency should remain isolated until compatibility is checked.
- On Windows, set or document `HF_HUB_DISABLE_SYMLINKS=1` for non-admin users.
- Inference should run in a worker thread/process and stream progress by consuming the VAD generator segment by segment.
- Cancellation needs to be considered because 15 minutes of audio took about 7 minutes on this CPU with the FP32 model.

## Current Status

Parakeet ONNX is integrated as a second optional STT backend. Remaining validation is live runtime testing from the Pandrator UI and packaged installer path with the real ONNX Parakeet environment and model cache.
