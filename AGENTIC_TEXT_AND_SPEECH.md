# Agentic text research and speech planning

Pandrator keeps viewer-facing text and synthesis-facing delivery as two related but independent branches:

```text
source
  → deterministic cleaning and optional agentic cleaning
  → correction
  → translation
  → final display/export artifact
       └→ structured speech plan
           → compiled TTS text
           → generation
```

Correction remains a subtitle-oriented stage for now. Translation may consume either the source or a corrected revision according to the selected workflow. The final accepted text revision remains the canonical source for subtitles and future read-along views. Speech planning never mutates it.

## Optional web research

Correction and LLM translation may opt into Jina-backed web research. Speech planning does not use web search.

The model operates a bounded search protocol:

1. Request a search query or finish.
2. Search through Jina and receive a small set of extracted results.
3. Optionally request extraction of a URL returned by that search.
4. Return evidence records that cite only URLs observed during the run.

Pandrator enforces search and extraction budgets, blocks local and non-public network destinations, caches provider responses, and treats all retrieved page text as untrusted data. A stage result stores its search steps, evidence records, URLs, and provider usage in an agent-run ledger. API credentials are resolved through the normal secret store and are never written into that ledger or the research cache.

DeepL translation is deterministic from Pandrator's perspective and cannot consume this LLM research context. The UI therefore requires either the LLM translation backend or disabled web research.

The initial provider is an internal Jina adapter:

- `s.jina.ai` performs search with extracted result content.
- `r.jina.ai` extracts a selected page.

This keeps the stage protocol independent of the transport. A future MCP adapter can implement the same internal provider interface without changing workflow state, prompts, or artifacts.

## Structured speech plans

Guarded mode is the default. Pandrator detects candidate spans such as abbreviations, numerals, symbols, Roman numerals, uppercase tokens, unfamiliar name shapes, and optional dictionary out-of-vocabulary terms. It assigns stable span identifiers, then asks the model for typed decisions:

- `keep`
- `verbalize`
- `pronounce`
- `spell_letters`
- `uncertain`

The model returns structured decisions and optional pronunciation discoveries. It does not need to rewrite the sentence. Pandrator validates span identifiers and decision types, applies accepted operations itself, and safely falls back to unchanged text if validation fails.

Flexible mode additionally asks for a complete speech sentence using protected placeholders. Pandrator validates placeholder preservation and minimum source retention. If the response is invalid, it retries through guarded planning before using the deterministic fallback.

A speech plan records:

- source hash, mode, model, display language, and voice language;
- candidate spans and model decisions;
- reused pronunciation entries and new proposals;
- compiled speech text;
- validation results and attempts.

Readable respellings retain their structure in the plan and library, for example `ee-mah-oh-kah`. The final compiler removes ASCII hyphens deterministically when the current backend needs plain speech text.

## Pronunciation library

Pronunciations are durable, editable records with a written form, language, optional voice language and backend, structured respelling, notes, scope, status, and revision.

- `proposed` entries are inactive until a user approves them.
- `reviewed` entries may be reused automatically.
- `disabled` entries remain available for audit and later restoration.
- session entries override matching global entries.

Model discoveries are de-duplicated and saved only as proposals. The generation review links directly to the library and shows which reviewed entries were reused.

## Invalidation and export behavior

Speech-plan reuse requires the same source text, planning mode, model, and effective reviewed-pronunciation revisions. Editing a generation segment clears its compiled speech text and plan. Editing a pronunciation causes affected plans to miss the cache on their next planning run.

Generation reads compiled speech text, while subtitle and display exports read the accepted display text. Whole-document planning writes a separate speech-plan sidecar and preserves the original display fields in structured artifacts.
