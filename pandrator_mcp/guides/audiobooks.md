# Audiobook workflow

An audiobook session starts from an uploaded, downloaded, or deliberately
reused document artifact. Its normal stages are:

1. **Clean source** — deterministic extraction with optional agent-assisted
   cleanup, producing reviewable clean text.
2. **Segment narration** — editable generation segments controlling text
   boundaries and pauses.
3. **Optimize narration** — optional, separate before-and-after text revision.
4. **Generate audio** — reviewable narration takes. Missing preparation may be
   included by an exact workflow plan.
5. **Assemble** — select takes and construct the intended audio sequence.
6. **Export** — package assembled audio with format, metadata, and cover choices.

Whole-document speech optimization and generation-time batch optimization are
alternative places to perform the same kind of transformation. Review the plan
and avoid enabling both unintentionally.

Generation can send narration text to a configured TTS provider. Cleanup or
optimization can send text to an LLM provider. A useful plan therefore states
which provider receives which data before execution. Generated takes and final
exports are different artifacts; generation does not imply final assembly.
