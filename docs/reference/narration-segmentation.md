# Narration lengths and native splitting

Choose the TTS model before segmenting an audiobook. Pandrator groups complete
sentences within a model-specific budget. Ordinary paragraphs stay separate;
short, confidently recognised dialogue turns can share a narration block.
Chapter and scene boundaries remain separate. These targets leave roughly 10%
below the reviewed application ceiling;
they are starting policies, not measured acoustic maxima for every checkpoint,
language, voice, or speaking style. Short paragraphs can remain shorter.

## Change the length in the UI

Open an audiobook project, select **Advanced**, and find **Length of new narration
blocks** in **Speech plan**. **Automatic · model default** displays the current
model target. Select **Custom limit**, enter the maximum characters, and click
**Save length**. Run **Segment narration** again and prepare a new speech plan to
apply it. Existing reviewed plans and recordings keep their boundaries.

Custom limits can exceed automatic targets. Actual model input and output limits
still apply. Irodori requests that cannot be shown to fit its text window fail
locally. Changing an output-token limit is not the same as changing a text limit.

## Dialogue flow and sentence packing

In **Segment narration → Settings**, **Dialogue flow** defaults to keeping short
dialogue turns together. English, Polish, French, Dutch, Japanese and German have
their own quotation and reporting-tag rules; Polish and French also recognise
conventional dash-led exchanges. Polish dash turns require an initial capital or
quotation mark; lowercase and numeric dash lists retain paragraph pauses. French
accepts unspaced em dashes and a
guillemet that opens a short, explicitly closed exchange across paragraphs.
English quotation runs also need evidence of an exchange: a question, a speaker
tag or an explicit reply. Personal pronouns and direct address alone do not
establish a conversation: they also occur in quoted examples and advertisements.
Quoted instructions, survey answers and collections of maxims keep their paragraph
pauses when exchange evidence is absent. Ambiguous passages can still need review
or AI annotation.
Other languages use conservative, balanced
quotation rules. Unknown passages retain their paragraph boundaries. This pass
uses local punctuation rules and makes no LLM requests or speaker assignments.
Choose the project's source language to use its specific profile. **Auto** or
an unknown language uses the generic quotation rules.

With one narrator, neighbouring recognised turns can share a block if the combined
text fits all active character, word and tokenizer-byte budgets. Their written
paragraph breaks remain in the block. With casting enabled, turns stay separate
for speaker review and use the sentence pause between them. The final turn keeps
the paragraph pause. Three or more authored newlines in plain text, explicit scene
ornaments and chapter markers stop grouping. Empty EPUB metadata wrappers do not
create scene gaps.

Packing keeps complete sentences in source order until the next sentence would
exceed a budget. An individually oversized sentence is split first: natural
punctuation is preferred, then conjunctions, then a whitespace fallback for Latin
text. Candidate splits aim for balanced parts, rather than a full block followed
by a tiny tail. Closing quotes stay with their punctuation; numeric commas are
not clause boundaries. CJK text requires a natural boundary and reports an error
if none fits. Existing chapter-title exemptions still apply.

## Added silence and playback

Audiobook defaults are **250 ms after a sentence block**, **700 ms after a
paragraph**, and **83 ms between pieces of a split sentence**. The paragraph pause
replaces the sentence pause. Explicit pauses, including zero, take precedence.
These pauses apply between generated blocks; sentences or dialogue turns inside
one provider request retain the provider's natural pacing.

New generated takes contain the provider audio without app-added silence. Assembly
adds the selected boundary and voice-change gaps. Changing pause settings can
therefore reuse these raw takes. Playlist playback follows the assembly pause
metadata, including internal voice-change gaps; individual auditions play raw
takes. Browser playback timing is approximate, while exported PCM assembly uses
frame positions. Neither adds a gap after the last playable block.

Unmarked historical takes may already contain voice-change padding; they retain
that audio and are not padded internally again. Existing frozen generation runs
retain their pause settings. For a new audiobook take with several render parts, RVC must
preserve its exact PCM duration so the internal boundaries remain valid; a
duration-changing conversion is rejected before publication.

Voiceover uses its own cue placement, slowdown, catch-up and synchronization gap
rules. Audiobook paragraph and dialogue pauses are not applied to that workflow.

EPUB data tables can be prepared as separate row paragraphs with published
headings repeated beside their values. Those rows retain paragraph boundaries
and use the paragraph pause; a long row may still need multiple speech blocks.
Unmarked layout tables retain plain row flow. Choose table and illustration
handling before preparing the source, under **Clean source → Settings**.
See [EPUB content policies](document-ingestion.md#tables-and-illustration-text).

## Optional AI dialogue annotation

Enable **Optimize text for speech**, open **Timing & settings**, and choose
**Optional AI dialogue recognition → Dialogue only**. **Annotate only** preserves
spoken wording and is selected automatically. This uses document preparation so
the result can be reviewed before generation. Speaker identification and delivery
analysis remain separate choices. AI annotation preserves existing speech blocks;
it does not automatically regroup a reviewed plan.

**Units per model request** controls direct annotation batches (1–64 units,
default 8), with a separate 24,000-source-character batch bound. It is still an
LLM pass over the supplied text, so punctuation-based preparation is the cheaper
starting point.

Through MCP, read and patch the project's `text` settings with
`pandrator_get_session_settings` and `pandrator_patch_session_settings`.
`audiobook_dialogue_flow` accepts `auto` or `off`. Passive speech-optimization
dispatch supports `annotation_mode="dialogue"` and `annotation_only=true`, with
configurable transport batches and resumable claims. Dialogue-only results cannot
introduce speaker identities, character proposals, delivery directions or vocal
events. Existing authored controls are preserved. Read each tool's returned next
actions for claiming, submitting and reviewing the revision.

## audio.cpp 0.9.2 defaults

The following are automatic audiobook targets in characters for ordinary Latin
script text. Language-specific targets are listed below. Quantizations of the
same family share the policy. A native chunk default is a runtime splitting
preference, not necessarily a hard model input limit.

| Family | Pandrator target | Native character chunk default |
| --- | ---: | ---: |
| `audio8_tts` | 180 | 200 |
| `auk` | 270 | Not a fixed request character default |
| `breeze_tts` | 540 | 600 |
| `chatterbox` | 180 | 128 |
| `chatterbox_turbo` | 180 | 128 |
| `confucius4_tts` | 216 | Not a fixed request character default |
| `cosyvoice3` | 540 | 600 |
| `dots_tts` | 288 | 320 |
| `dramabox` | 270 | Not a fixed request character default |
| `echo_tts` | 270 | 300 |
| `f5_tts` | 270 | Not a fixed request character default |
| `firered_audio` | 270 | Not a fixed request character default |
| `fireredtts3` | 540 | 600 |
| `fish_audio` | 270 | 200 |
| `glm_tts` | 270 | Not a fixed request character default |
| `higgs_audio_tts` | 921 | 1024 |
| `index_tts2` | 270 | Not a fixed request character default |
| `inflect_v2` | 252 | 280 |
| `irodori_tts` | 72 | Not a fixed request character default |
| `kitten_tts` | 360 | 400 |
| `kitten_tts2` | 342 | 380 |
| `kokoro_tts` | 216 | 240 |
| `kugelaudio` | 270 | 300 |
| `lfm2_audio` | 180 | 200 |
| `magpie_tts` | 270 | 300 |
| `maya1` | 270 | 300 |
| `miotts` | 270 | Not a fixed request character default |
| `moss_tts_local` | 1,843 | 2048 |
| `moss_tts_nano` | 230 | 256 |
| `moss_tts_v15` | 180 | 200 |
| `moss_ttsd` | 270 | Not a fixed request character default |
| `moss_voicegen` | 180 | 200 |
| `neutts` | 540 | 600 |
| `omnivoice` | 270 | Not a fixed request character default |
| `outetts` | 230 | 256 |
| `piper_tts` | 252 | 280 |
| `pocket_tts` | 180 | Not a fixed request character default |
| `qwen3_tts` | 1,800 | 8192 |
| `sanotts` | 252 | 280 |
| `soprano_tts` | 180 | 200 |
| `sopro_tts` | 270 | Not a fixed request character default |
| `supertonic` | 270 | Not a fixed request character default |
| `vibevoice` | 270 | Not a fixed request character default |
| `voxcpm1` | 1,800 | 2048 |
| `voxcpm2` | 1,800 | 2048 |
| `voxtral_tts` | 1,152 | 300 |
| `zipvoice` | 180 | 128 |

Voxtral also has a **240-word** automatic target, below Mistral's recommendation
of fewer than 300 words. Its default 2,048 output frames represent 80 ms each;
they are not text tokens. Pocket uses an explicit character override; its native
50-text-token fallback otherwise also splits at sentence boundaries. Confucius'
native 80-token splitter is replaced by the framework character path.

Qwen uses 450 characters for Chinese, Japanese, and Korean; VoxCPM uses 450;
Breeze 324; Higgs 270; MOSS Local 450; and MOSS Nano 72. Supertonic uses 108 for
Japanese and Korean. Confucius uses 72 for Chinese, Japanese, and Korean.
Korean Chatterbox uses 63: its default 384 speech tokens allow only about
15 seconds at the multilingual decoder's 25 tokens per second.
Irodori uses 72 characters and a 225-byte normalized-tokenizer budget: spaces
expand to a three-byte metaspace marker, and the request guard reserves BOS and
the v3 prefix within the 256-token checkpoint window. This conservative bound
can reject text that a full tokenizer would compress more efficiently.

Explicitly lower Qwen and Breeze output budgets reduce their automatic character
targets. A smaller saved `text_chunk_size` also reduces automatic preparation
(except Confucius, whose default setting has different units). Custom mode
keeps the chosen character count; final request guards still apply.

## Native chunking and exceptions

For models with request-level character chunking, Pandrator supplies a chunk
budget that covers the complete compiled speech text, including inline controls.
Native `endline` mode is replaced with `tag_aware`; Confucius uses `tag_aware` to
bypass its separate token/sentence splitter. Pocket and OmniVoice receive explicit
character budgets to bypass their automatic sentence/duration paths. DramaBox's
long-form threshold is raised without changing its requested output duration.

Soprano exposes chunk size only when its session is created. Manager configures
`session_options.text_chunk_size: 65536`; externally managed servers need the
same setting. This only bypasses text splitting and does not enlarge the model's
output-frame or context window.

F5 and OuteTTS are available for narration, with a limitation: audio.cpp 0.9.2
exposes no way to disable F5's reference-dependent splitter or OuteTTS's
context/output retry splitting. Pandrator still prepares and saves its own
segments, and uses a full-segment character budget where supported, but the
runtime may divide the text again inside one request. Its successful HTTP
responses do not report these sub-splits. Exact internal boundaries and pauses
therefore cannot be guaranteed. Use another model when precise sentence timing
or control over every internal pause matters. Existing recordings remain usable.

With other families, model-native context checks and output ceilings still apply.
Use a representative multi-sentence audition before preparing a long book,
especially after changing language, speed, reference audio, or output budgets.

Sources: [pinned model specs](https://github.com/0xShug0/audio.cpp/tree/0097d365a00b52a6991ad7618f0a04e8d2935e47/model_specs),
[framework splitter](https://github.com/0xShug0/audio.cpp/blob/0097d365a00b52a6991ad7618f0a04e8d2935e47/src/framework/text/chunking.cpp),
[Voxtral guidance](https://docs.mistral.ai/studio/audio/text_to_speech/speech).
