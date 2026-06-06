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
- Do not assume standard EPUB structure. Books may use semantic headings, arbitrary classes/ids, plain paragraphs, blockquotes, spans, formatting-only markup, inaccurate navigation, or very large multi-section documents.
- Treat navigation, tags, classes, ids, roles, text patterns, and position as independent evidence. Confirm important actions with a second signal or a preview.
- Use previews and markup inspection before deleting substantial text.
- Keep inspections efficient: batch independent inspections, preview representative blocks, and request raw markup only when plain structural metadata is insufficient.
- Prefer one batch action containing several independent inspections over several consecutive single-tool turns. Do not batch an inspection that depends on a previous result.
- The initial source overview already contains deterministic structure, cleanup, and chapter hypotheses. Use it to choose targeted previews instead of rediscovering the same facts.
- Prefer selector-based cleanup over long block_id lists when a whole TOC/nav/license/notes section shares href, class, id, tag, role, or text pattern.
- Selectors may combine href, href_regex, tag, class/classes, element_id, element_id_regex, role/roles, text_regex, start_line/end_line, and exclusion fields.
- Inspect every distinct TOC variant. EPUBs often contain both an inline contents list and a separate navigation document; removing one does not remove the other.
- When removing boilerplate or a license, remove the complete confirmed section/document. Deleting only its heading is not sufficient.
- Preserve the book's real title/byline and closing markers such as "THE END" unless there is strong evidence they are unwanted duplicates. Avoid broad ranges that cross into them.
- Calibrate confidence to verified completeness; do not claim near-certain confidence while cleanup candidate groups remain uninspected or only section headings were deleted.
- Before finishing a book-length source, inspect chapter structure and verify chapter-marker completeness against navigation titles and likely heading candidates.
- Do not mark only the first/last examples of a repeated chapter pattern. Preview the full selector and use mark_chapters_by_selector when it safely covers the complete narrative heading set.
- Mark real chapter/part/opening section headings with [[Chapter]] via mark_chapter or mark_chapters_by_selector operations.
- Remove non-audiobook material: TOC-as-list, copyright boilerplate, ads, image alt text, captions, running headers/footers, page numbers, and optional notes.
- Preserve narrative prose unless there is strong evidence it should not be read aloud.
- Finish is automatically evaluated by deterministic guards. Call evaluate_operations only when you want feedback before committing to a finish proposal.
- {footnote_policy}

You must answer every turn with one JSON object and no prose.

Inspection command schema:
{{"action":"batch","arguments":{{"commands":[{{"action":"preview","arguments":{{"start_line":1,"end_line":30,"max_blocks":12}}}},{{"action":"preview_selector","arguments":{{"selector":{{"class":"toc"}},"max_blocks":8}}}}]}}}}
{{"action":"inspect_document_structure","arguments":{{"max_documents":30,"scope":{{"start_line":1,"end_line":500}}}}}}
{{"action":"inspect_navigation","arguments":{{"max_entries":80,"max_matches_per_entry":5}}}}
{{"action":"search","arguments":{{"query":"text OR other text","mode":"plain","case_sensitive":false,"max_hits":20}}}}
{{"action":"regex_search","arguments":{{"pattern":"...","flags":"i","max_hits":20}}}}
{{"action":"preview","arguments":{{"start_line":1,"end_line":20,"max_blocks":12}}}}
{{"action":"preview","arguments":{{"around_hit_id":"hit:1","before":5,"after":8}}}}
{{"action":"inspect_block","arguments":{{"block_id":"..."}}}}
{{"action":"get_epub_markup_for_text","arguments":{{"text":"chapter title","occurrence":1,"context_blocks":2}}}}
{{"action":"preview_raw_markup_range","arguments":{{"start_line":1,"end_line":20,"max_blocks":20}}}}
{{"action":"list_epub_selectors","arguments":{{"min_count":2,"max_items":80}}}}
{{"action":"preview_selector","arguments":{{"selector":{{"href":"toc.xhtml"}},"max_blocks":30,"include_raw_markup":true}}}}
{{"action":"list_repeated_lines","arguments":{{"min_repeats":3}}}}
{{"action":"find_heading_candidates","arguments":{{"max_candidates":80}}}}
{{"action":"analyze_chapter_structure","arguments":{{"max_candidates":80}}}}
{{"action":"analyze_cleanup_structure","arguments":{{"max_candidates":20}}}}
{{"action":"find_footnote_candidates","arguments":{{"max_candidates":80}}}}
{{"action":"find_metadata_candidates","arguments":{{}}}}
{{"action":"evaluate_operations","arguments":{{"operations":[{{"op":"delete_range","start_line":1,"end_line":5,"reason":"confirmed front matter"}}]}}}}

Final command schema:
{{
  "action": "finish",
  "summary": "brief explanation of evidence and cleaning strategy",
  "confidence": 0.0,
  "operations": [
    {{"op":"set_metadata","title":"...","author":"...","language":"..."}},
    {{"op":"delete_range","start_line":1,"end_line":12,"reason":"copyright boilerplate"}},
    {{"op":"delete_blocks","block_ids":["..."],"reason":"image alt text"}},
    {{"op":"delete_by_selector","selector":{{"href":"toc.xhtml"}},"reason":"EPUB navigation/TOC document, confirmed by preview_selector"}},
    {{"op":"delete_by_selector","selector":{{"class":"footnote"}},"reason":"footnote class, confirmed by preview_selector"}},
    {{"op":"mark_chapter","block_id":"...","title":"Chapter title"}},
    {{"op":"mark_chapters_by_selector","selector":{{"role":"heading","text_regex":"^CHAPTER\\\\s+[IVXLCDM]+\\\\."}},"reason":"complete narrative chapter pattern, confirmed by preview_selector"}},
    {{"op":"replace_range","start_line":10,"end_line":10,"replacement":"small OCR fix","reason":"small OCR fix"}}
  ]
}}
""".strip()


def build_initial_user_prompt(
    document: SourceDocument,
    source_overview: dict | None = None,
) -> str:
    first_blocks = [
        {
            "block_id": block.block_id,
            "line": block.line_start,
            "text": _short_text(block.text),
            "href": block.href,
            "page": block.page,
            "tag": block.tag,
            "classes": block.classes,
            "roles": block.role_candidates,
        }
        for block in document.blocks[:12]
    ]
    last_blocks = [
        {
            "block_id": block.block_id,
            "line": block.line_start,
            "text": _short_text(block.text),
            "href": block.href,
            "page": block.page,
            "tag": block.tag,
            "classes": block.classes,
            "roles": block.role_candidates,
        }
        for block in document.blocks[-8:]
    ]
    summary = {
        "source_type": document.source_type,
        "filename": document.filename,
        "language_hint": document.language,
        "block_count": len(document.blocks),
        "metadata_candidates": _compact_metadata_candidates(document.metadata_candidates),
        "nav_titles_preview": document.nav_titles[:20],
        "source_overview": source_overview or {},
        "warnings": [_short_text(item) for item in document.warnings[:20]],
        "first_blocks": first_blocks,
        "last_blocks": last_blocks,
    }
    return (
        "Inspect this source and prepare it for audiobook/TTS cleanup. "
        "Start by choosing inspection tools based on the source overview. Do not assume the source uses "
        "semantic headings or accurate navigation. "
        "When ready, call finish with targeted operations.\n\n"
        + json.dumps(summary, ensure_ascii=False, indent=2)
    )


def _short_text(text: str, max_chars: int = 240) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 3] + "..."


def _compact_metadata_candidates(candidates: dict) -> dict:
    compact: dict[str, list[dict]] = {}
    for key, values in candidates.items():
        if not isinstance(values, list):
            continue
        compact[str(key)] = [
            {
                field: _short_text(value) if field == "value" else value
                for field, value in candidate.items()
                if field in {"value", "source", "confidence"}
            }
            for candidate in values[:5]
            if isinstance(candidate, dict)
        ]
    return compact
