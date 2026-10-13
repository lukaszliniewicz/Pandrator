# Provider model lifecycle

Pandrator can retain a model in a saved project after its provider announces a
shutdown. The model catalogue shows deprecation information beside the selected
model. Your settings stay unchanged until you explicitly choose a replacement.
Provider account access and a healthy connection remain separate from catalogue
support.

## Announced TTS changes

| Provider | Affected model IDs | Shutdown |
| --- | --- | --- |
| Gemini API | `gemini-2.5-flash-preview-tts`, `gemini-2.5-pro-preview-tts`, `gemini-3.1-flash-tts-preview` | 17 November 2026 |
| OpenAI API | `tts-1`, `tts-1-hd`, `gpt-4o-mini-tts-2025-03-20`, `gpt-4o-mini-tts-2025-12-15` | 6 January 2027 |
| ElevenLabs | `eleven_turbo_v2`, `eleven_turbo_v2_5` | Deprecated; no exact shutdown date is listed |

Google recommends `gemini-3.8-flash-tts` or `gemini-3.8-flash-lite-tts` for the
listed Gemini API models. Both are registered in Pandrator. Select a replacement
and an appropriate voice, try a short passage, and listen before continuing a
long project. These Gemini API dates do not establish retirement dates for
different Vertex or Cloud Text-to-Speech model IDs.
[Google lifecycle information](https://ai.google.dev/gemini-api/docs/deprecations).

OpenAI recommends `gpt-realtime-2.1-mini` through its Realtime API. That route
generates an assistant response from instructions and input text. The dedicated
Speech API has a different literal-text request contract, and its accepted-model
schema does not list the recommended Realtime model. Pandrator therefore keeps
the existing Speech API selections while showing the migration notice; it does
not automatically substitute a conversational response for narration. The
unversioned `gpt-4o-mini-tts` model page marks that alias deprecated without
assigning it a separate shutdown date in the retirement table.
[OpenAI lifecycle information](https://developers.openai.com/api/docs/deprecations),
[Speech API](https://developers.openai.com/api/reference/resources/audio/subresources/speech/methods/create),
[Realtime conversations](https://developers.openai.com/api/docs/guides/realtime-conversations),
[alias information](https://developers.openai.com/api/docs/models/gpt-4o-mini-tts).

ElevenLabs recommends the corresponding `eleven_flash_v2` and
`eleven_flash_v2_5` models. Choose the replacement explicitly; the model overview
does not provide an exact Turbo shutdown date.
[ElevenLabs lifecycle information](https://elevenlabs.io/docs/overview/models).

The catalogue information was checked on 13 October 2026. Follow the provider
links for later changes. Saved audio, project history, model settings, and
provider availability are independent: changing a model for future generation
does not rewrite earlier run settings.
