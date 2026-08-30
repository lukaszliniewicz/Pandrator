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

PDF layout/OCR, EPUB spine and navigation handling, deterministic and optional
model-assisted cleanup, artifacts, and narration segmentation are described in
the public [document-ingestion reference](https://github.com/lukaszliniewicz/Pandrator/blob/main/docs/reference/document-ingestion.md).
The optional cleanup model is run by Pandrator through its configured provider;
it is not a passive MCP batch. Passive dispatch currently applies to subtitle
correction and translation.

Generation can send narration text to a configured TTS provider. Cleanup or
optimization can send text to an LLM provider. A useful plan therefore states
which provider receives which data before execution. Generated takes and final
exports are different artifacts; generation does not imply final assembly.
