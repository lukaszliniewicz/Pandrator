# Pronunciation and speech text

Pandrator keeps display text separate from the text sent to a speech provider.
That distinction allows speech-specific respelling, abbreviation expansion,
or optimization without silently changing subtitles, chapters, or source
documents.

## Three different edits

| Mechanism | What changes | When to use it |
| --- | --- | --- |
| Edit or Search/Replace | The selected source, display, or speech revision | The stored text itself is wrong |
| Pronunciation library | Only the synthesized request payload | A known written term needs a deterministic spoken form |
| Speech-text optimization | A separate, reviewable speech revision | Broader provider-specific preparation needs language judgment |

Do not use pronunciation rules to conceal errors that should be corrected in
the actual document or subtitle track.

## Add a pronunciation

1. Open **Pronunciations** and add the written form plus a lowercase structured
   respelling.
2. Leave it as **Needs review** until you have listened to it.
3. Approve it as **Reviewed** and choose global or one-session scope.
4. Optionally restrict it to a language and TTS backend. `und` matches any
   language and `*` matches any backend.
5. In the session's deterministic text settings, keep **Apply reviewed
   pronunciation-library overrides** enabled.

Only reviewed entries in the applicable scope are active. Matching is bounded,
case-insensitive, longest-first, and non-overlapping. The display text remains
unchanged; the replacement occurs when Pandrator prepares a synthesis request.

After changing an entry, regenerate affected audio. If you deliberately reuse
an older saved speech plan, rebuild that plan too: Pandrator cannot infer and
reverse respellings already stored in a historical speech revision.

## Speech-text optimization

Optimization can prepare numbers, abbreviations, punctuation, pronunciation,
or phrasing for a particular TTS service. It uses the configured
multi-provider LLM adapter; no particular provider such as Ollama is required.
The result is a distinct speech revision that must be reviewed.

Whole-document optimization and generation-time batch optimization are two
places to perform similar work. Avoid enabling both accidentally. If an LLM
proposes a pronunciation-library entry, it remains inactive until you review
and approve it.

## Mixed-language speech

Most local wrappers accept one language for one synthesis request. Tags such
as `[en]…[/en]` are normally sent literally and are not a portable
code-switching protocol.

When practical, isolate a foreign phrase as its own segment and use a
per-segment language/voice override or **Generate alternate take**. The
alternate take can select provider, model, voice, language, speech prompt, and
RVC settings when the service supports them. A compatible multilingual voice
may work across the selected languages; otherwise choose an appropriate voice
for each segment.

Surgical regeneration of an inline word or phrase would require reliable span
alignment, a compatible voice in both languages, silence and duration matching,
and review confidence. Do not assume that Pandrator currently performs that
splice automatically.

## Review by listening

Text review cannot establish whether a speech-specific transformation sounds
right. Generate short representative takes and listen for:

- names, numbers, dates, acronyms, and abbreviations;
- language transitions;
- unwanted literal punctuation or tags;
- stress, rhythm, and pauses;
- regressions caused by a broader replacement; and
- differences between original and RVC-converted takes.

For the surrounding generation flow, see
[your first audiobook](../getting-started/first-audiobook.md) and
[your first voiceover](../getting-started/first-voiceover.md).
