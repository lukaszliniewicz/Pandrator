from __future__ import annotations

import json

from .models import SourceDocument


def build_system_prompt(remove_footnotes: bool = False) -> str:
    footnote_policy = (
        "The user requested footnote/endnote removal. Remove likely footnotes/endnotes when evidence is strong."
        if remove_footnotes
        else "The user did not request footnote/endnote removal. Do not remove footnotes unless they are clearly non-narrative boilerplate."
    )
    return f"""
You are preparing extracted book text for TTS/audiobook generation.
Your job is to inspect the source with tools, then propose targeted deterministic operations.

Core rules:
- Do not rewrite the whole book.
- Do not invent metadata. Use evidence from filename, EPUB metadata, title pages, or source text.
- Work across languages. Do not rely only on English words like "chapter" or "contents".
- Treat EPUB classes/ids as hints, not truth; many are non-semantic.
- Use previews and markup inspection before deleting substantial text.
- Mark real chapter/part/opening section headings with [[Chapter]] via mark_chapter operations.
- Remove non-audiobook material: TOC-as-list, copyright boilerplate, ads, image alt text, captions, running headers/footers, page numbers, and optional notes.
- Preserve narrative prose unless there is strong evidence it should not be read aloud.
- {footnote_policy}

You must answer every turn with one JSON object and no prose.

Inspection command schema:
{{"action":"search","arguments":{{"query":"text OR other text","mode":"plain","case_sensitive":false,"max_hits":20}}}}
{{"action":"regex_search","arguments":{{"pattern":"...","flags":"i","max_hits":20}}}}
{{"action":"preview","arguments":{{"start_line":1,"end_line":20}}}}
{{"action":"preview","arguments":{{"around_hit_id":"hit:1","before":5,"after":8}}}}
{{"action":"inspect_block","arguments":{{"block_id":"..."}}}}
{{"action":"get_epub_markup_for_text","arguments":{{"text":"chapter title","occurrence":1,"context_blocks":2}}}}
{{"action":"list_repeated_lines","arguments":{{"min_repeats":3}}}}
{{"action":"find_heading_candidates","arguments":{{"max_candidates":80}}}}
{{"action":"find_footnote_candidates","arguments":{{"max_candidates":80}}}}
{{"action":"find_metadata_candidates","arguments":{{}}}}

Final command schema:
{{
  "action": "finish",
  "summary": "brief explanation of evidence and cleaning strategy",
  "confidence": 0.0,
  "operations": [
    {{"op":"set_metadata","title":"...","author":"...","language":"..."}},
    {{"op":"delete_range","start_line":1,"end_line":12,"reason":"copyright boilerplate"}},
    {{"op":"delete_blocks","block_ids":["..."],"reason":"image alt text"}},
    {{"op":"mark_chapter","block_id":"...","title":"Chapter title"}},
    {{"op":"replace_range","start_line":10,"end_line":10,"replacement":"small OCR fix","reason":"small OCR fix"}}
  ]
}}
""".strip()


def build_initial_user_prompt(document: SourceDocument) -> str:
    first_blocks = [
        {
            "block_id": block.block_id,
            "line": block.line_start,
            "text": block.text,
            "href": block.href,
            "page": block.page,
            "tag": block.tag,
            "classes": block.classes,
            "roles": block.role_candidates,
        }
        for block in document.blocks[:35]
    ]
    last_blocks = [
        {
            "block_id": block.block_id,
            "line": block.line_start,
            "text": block.text,
            "href": block.href,
            "page": block.page,
            "tag": block.tag,
            "classes": block.classes,
            "roles": block.role_candidates,
        }
        for block in document.blocks[-20:]
    ]
    summary = {
        "source_type": document.source_type,
        "filename": document.filename,
        "language_hint": document.language,
        "block_count": len(document.blocks),
        "metadata_candidates": document.metadata_candidates,
        "nav_titles_preview": document.nav_titles[:40],
        "warnings": document.warnings,
        "first_blocks": first_blocks,
        "last_blocks": last_blocks,
    }
    return (
        "Inspect this source and prepare it for audiobook/TTS cleanup. "
        "Start by gathering enough evidence with tools. "
        "When ready, call finish with targeted operations.\n\n"
        + json.dumps(summary, ensure_ascii=False, indent=2)
    )
