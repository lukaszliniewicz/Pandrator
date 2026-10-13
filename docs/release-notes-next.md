# Next release

These changes are in development and are not included in the published 0.12.0
release.

## Captions and speech

Compare subtitles opens the project's selected written captions. The editor
shows which version is editable and whether it matches the selected captions.
Adding a comparison or choosing another editable version keeps the project's
output selection intact.

Exports now offer an explicit caption text source. **Current display text** is
the default, independently of the selected audio version. Choose **Historical
text from selected audio version** to use the wording captured with a previous
generation run. This changes the earlier behavior that used the selected run's saved
wording automatically. Subtitle-only exports can use current text without an
audio run. Queued exports keep their captured wording if text changes later.

Display-only edits retain the existing pronunciation and recordings when the
spoken text is unchanged. Historical audio versions keep their saved text.
See [formats and exports](reference/formats-and-exports.md) and
[generation controls](reference/generation-controls.md).

Japanese and Chinese subtitle layout now uses word boundaries together with
sentence and clause punctuation to avoid awkward splits. Traditional Chinese
retains its original script, and other languages retain their existing layout
rules. Width and timing limits still apply.

## Covers, models, and background work

- M4B exports convert WebP covers to genuine PNG artwork, including transparency.
  JPEG and PNG artwork is identified by its contents. The uploaded cover stays
  intact.
- Background work shows running and queued jobs separately, identifies running
  transcription work, and reports the available worker-health evidence.
- Required-reference audio.cpp models expose consistent reference metadata.
  MCP catalogue responses distinguish advertised models, service availability,
  and unknown health.
- Provider settings show lifecycle warnings for deprecated models and announced
  shutdowns. Saved model choices stay intact; migration remains explicit.
  See [provider lifecycle and migrations](reference/provider-lifecycle.md).
- Existing VTT subtitles can be registered as a subtitle source before
  correction or translation. See [your first subtitles](getting-started/first-subtitles.md).
- YuE2 offers an instrumental mode using a separately downloaded, verified AR
  adapter. The adapter is licensed for noncommercial use. Ready sound candidates
  and saved clip variants also have direct download controls. See
  [sound and music generation](reference/audio-generation-models.md).
