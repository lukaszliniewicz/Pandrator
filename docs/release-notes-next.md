# Next release

These changes are in development and are not included in the published 0.12.0
release.

## Captions and speech

Audiobook preparation recognises short dialogue exchanges with local rules for
English, Polish, French, Dutch, Japanese and German, plus conservative quotation
rules for other languages. Compatible turns can share a narration block with one
narrator. Oversized sentences split at balanced natural boundaries. Optional AI
dialogue annotation is available independently of casting and delivery analysis.
English quoted passages require a question, explicit reply or reporting tag;
personal pronouns alone do not join quoted examples, advertisements or survey answers.
Polish lowercase and numeric dash lists also retain paragraph pauses.

New raw takes contain no app-added silence. Assembly applies audiobook pauses,
and playlist playback follows the same pause metadata; individual auditions remain
raw. Historical padded takes are preserved. Voiceover retains its separate cue
timing and synchronization rules. EPUB extraction also excludes ruby readings
from written text and prevents empty metadata wrappers from creating scene gaps.
See [narration preparation and pauses](reference/narration-segmentation.md).

EPUB preparation now offers table and illustration policies. The defaults retain
meaningful supplied descriptions and captions, avoid duplicate alternatives, and
read data rows with their published headings. Unmarked layout tables retain
plain row order. Table content and illustration text can be controlled in source
settings or passive MCP requests. Re-preparing the source applies the policy;
existing text and recordings remain available. See [EPUB content
policies](reference/document-ingestion.md#tables-and-illustration-text).

EPUB note extraction preserves inline anchor positions and container boundaries,
preventing one note from absorbing later notes or the narrative after a
marker-only note target. A book-scoped path index avoids
repeated scans of every document for each reference. Fallback sentence splitting
also reuses its language configuration between paragraphs.

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

- audio.cpp moves to 0.9.2 with digest-pinned runtime archives and refreshed
  model metadata. New speech options include Voxtral 4B TTS and French PocketTTS.
  Voxtral is licensed for noncommercial use; French PocketTTS defaults to the
  bundled `cosette` voice and temperature 0.3.
- Automatic narration lengths now follow reviewed audio.cpp family policies
  with headroom. Ordinary Chatterbox, Turbo, and ZipVoice targets are 180
  characters; Voxtral uses 1,152 characters and up to 240 words. Native sentence
  splitting is bypassed on supported routes. F5 and OuteTTS remain usable with
  documented limits on internal boundaries and pauses. Irodori validates
  its token window before synthesis. Existing plans and recordings remain intact.
  See [narration lengths](reference/narration-segmentation.md).
- Kokoro offers an optional **Japanese support** installation setting. It adds
  verified private MeCab and UniDic resources, prepares a separate GGUF without
  changing the original model's tensor bytes or precision, and works with
  existing installations. The setting defaults to off and explains the extra
  502 MiB download and disk requirements. See
  [Japanese with Kokoro](guides/providers-and-voices.md#japanese-with-kokoro).
  Adding another model keeps the saved Japanese-support choice.
- Preset voices share readable names and language metadata across the voice
  library and project selectors. Voxtral prefers language-matched defaults while
  retaining cross-language choices. Pocket's English safetensors voices and
  classic Kitten's eight voices are listed, and fresh Supertonic, Kitten TTS 2,
  and NeuTTS defaults follow their native defaults. British Kokoro voices retain
  the correct phonemizer language. Saved choices and voice identifiers stay intact.
  First-class audio.cpp requests retain configured defaults for each language.
  See [preset voices](guides/providers-and-voices.md#preset-voice-names-and-languages).
- M4B exports convert WebP covers to genuine PNG artwork, including transparency.
  JPEG and PNG artwork is identified by its contents. The uploaded cover stays
  intact.
- Background work shows running and queued jobs separately, identifies running
  transcription work, and reports the available worker-health evidence.
- Required-reference audio.cpp models expose consistent reference metadata.
  MCP catalogue responses distinguish advertised models, service availability,
  and unknown health.
- Opening provider and voice controls uses local Google application-default
  credential discovery without waiting for cloud metadata probes. An explicit
  provider refresh retains network discovery.
- Automatic assembly batches take-metadata lookups, and generation views reuse
  their resolved settings instead of reading them twice.
- Provider settings show lifecycle warnings for deprecated models and announced
  shutdowns. Saved model choices stay intact; migration remains explicit.
  See [provider lifecycle and migrations](reference/provider-lifecycle.md).
- Existing VTT subtitles can be registered as a subtitle source before
  correction or translation. See [your first subtitles](getting-started/first-subtitles.md).
- YuE2 offers an instrumental mode using a separately downloaded, verified AR
  adapter. The adapter is licensed for noncommercial use. Ready sound candidates
  and saved clip variants also have direct download controls. See
  [sound and music generation](reference/audio-generation-models.md).

Source archives recursively exclude private working notes and review files,
including nested example environment files.
