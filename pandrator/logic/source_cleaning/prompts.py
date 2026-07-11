from __future__ import annotations

import json

from .models import SourceDocument


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_system_prompt(remove_footnotes: bool = False, phase_name: str = "full") -> str:
    if phase_name == "metadata":
        return _metadata_system_prompt()
    if phase_name == "navigation":
        return _navigation_system_prompt()
    if phase_name == "boilerplate":
        return _boilerplate_system_prompt(remove_footnotes)
    if phase_name == "repeated_elements":
        return _repeated_elements_system_prompt()
    if phase_name == "chapter_marking":
        return _chapter_marking_system_prompt()
    # "full" — legacy single-pass prompt (backward compat)
    return _full_system_prompt(remove_footnotes)


def build_initial_user_prompt(
    document: SourceDocument,
    source_overview: dict | None = None,
    phase_name: str = "full",
    previous_phase_summaries: list[dict] | None = None,
) -> str:
    if phase_name == "metadata":
        return _metadata_user_prompt(document, previous_phase_summaries)
    if phase_name == "navigation":
        return _navigation_user_prompt(document, source_overview, previous_phase_summaries)
    if phase_name == "boilerplate":
        return _boilerplate_user_prompt(document, source_overview, previous_phase_summaries)
    if phase_name == "repeated_elements":
        return _repeated_elements_user_prompt(document, previous_phase_summaries)
    if phase_name == "chapter_marking":
        return _chapter_marking_user_prompt(document, source_overview, previous_phase_summaries)
    # "full" legacy
    return _full_user_prompt(document, source_overview)


# ---------------------------------------------------------------------------
# Phase 1 — Metadata extraction
# ---------------------------------------------------------------------------

def _metadata_system_prompt() -> str:
    return """
You are extracting book metadata for TTS/audiobook preparation.

Your ONLY job: identify the book's title, author name, language, and optionally genre.

Rules:
- Do NOT delete any text.
- Do NOT mark chapters.
- Only propose set_metadata operations.
- Use evidence from: filename, EPUB metadata candidates, title pages, copyright page.
- Do not invent metadata. Only use what is clearly stated in the source.
- If you cannot determine a field with confidence, omit it rather than guessing.

You must answer every turn with one JSON object and no prose.

Available commands:
{"action":"find_metadata_candidates","arguments":{}}
{"action":"preview","arguments":{"start_line":1,"end_line":30,"max_blocks":15}}
{"action":"search","arguments":{"query":"by OR written by OR author","mode":"plain","case_sensitive":false,"max_hits":10}}
{"action":"batch","arguments":{"commands":[{"action":"find_metadata_candidates","arguments":{}},{"action":"preview","arguments":{"start_line":1,"end_line":20,"max_blocks":10}}]}}

Finish command — the ONLY accepted operation is set_metadata:
{
  "action": "finish",
  "summary": "brief explanation of sources used",
  "confidence": 0.85,
  "operations": [
    {"op":"set_metadata","title":"Book Title","author":"Author Name","language":"en","genre":"Fiction"}
  ]
}

If metadata cannot be determined with confidence, finish with an empty operations list.
""".strip()


def _metadata_user_prompt(
    document: SourceDocument,
    previous_phase_summaries: list[dict] | None = None,
) -> str:
    first_blocks = [
        {
            "block_id": b.block_id,
            "line": b.line_start,
            "text": _short_text(b.text),
            "href": b.href,
            "tag": b.tag,
            "classes": b.classes,
        }
        for b in document.blocks[:20]
    ]
    summary = {
        "source_type": document.source_type,
        "filename": document.filename,
        "language_hint": document.language,
        "block_count": len(document.blocks),
        "metadata_candidates": _compact_metadata_candidates(document.metadata_candidates),
        "nav_titles_preview": document.nav_titles[:10],
        "first_blocks": first_blocks,
    }
    parts = [
        "Extract metadata for this source. Use find_metadata_candidates first, "
        "then inspect front matter as needed.\n\n" + json.dumps(summary, ensure_ascii=False, indent=2)
    ]
    if previous_phase_summaries:
        parts.append("\nPrevious phases:\n" + json.dumps(previous_phase_summaries, ensure_ascii=False, indent=2))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Phase 2 — Navigation / TOC removal
# ---------------------------------------------------------------------------

def _navigation_system_prompt() -> str:
    return """
You are preparing extracted book text for TTS/audiobook generation.

Your ONLY job: identify and delete table-of-contents sections and navigation documents.

WHAT TO DELETE:
- EPUB navigation documents — entire href files used only for navigation (nav.xhtml, toc.ncx content, etc.)
- Inline "Table of Contents" or "Contents" sections — lists of chapter titles, typically with page numbers or dot-leaders
- Any structured list of chapter/section links that serves as navigation, not as narrative
- ALL variants: some EPUBs have both a standalone nav document AND an inline TOC; remove both

WHAT NOT TO DELETE:
- Chapter headings that appear in the narrative body (these will be marked in a later phase)
- The book's prose or narrative text
- Title pages, dedications, epigraphs
- Short "See also" references within prose

Rules:
- Blocks/documents pre-annotated with `deterministic_toc` in their roles should be prioritized. Audit them for correctness and delete them if they represent TOC/navigation sections.
- Confirm suspected TOC sections with preview_selector or preview before deleting.
- Prefer delete_by_selector for document-scope (href) or class-scope targets.
- Use delete_range only for confirmed inline TOC sections you have previewed.
- Work across languages — TOC sections exist in all languages.
- If the source has no detectable TOC, finish with an empty operations list.

You must answer every turn with one JSON object and no prose.

Available commands:
{"action":"inspect_document_structure","arguments":{"max_documents":30}}
{"action":"list_epub_selectors","arguments":{"min_count":2,"max_items":80}}
{"action":"preview_selector","arguments":{"selector":{"href":"toc.xhtml"},"max_blocks":20}}
{"action":"preview_selector","arguments":{"selector":{"role":"toc"},"max_blocks":20}}
{"action":"preview","arguments":{"start_line":1,"end_line":40,"max_blocks":20}}
{"action":"analyze_cleanup_structure","arguments":{"max_candidates":15}}
{"action":"inspect_navigation","arguments":{"max_entries":60}}
{"action":"search","arguments":{"query":"contents OR table of contents","mode":"plain","case_sensitive":false,"max_hits":10}}
{"action":"regex_search","arguments":{"pattern":"...","flags":"i","max_hits":20}}
{"action":"batch","arguments":{"commands":[...]}}

Finish command — only delete_range, delete_blocks, delete_by_selector are accepted:
{
  "action": "finish",
  "summary": "brief description of what was found and deleted",
  "confidence": 0.9,
  "operations": [
    {"op":"delete_by_selector","selector":{"href":"toc.xhtml"},"reason":"EPUB navigation document, confirmed by preview_selector"},
    {"op":"delete_range","start_line":10,"end_line":45,"reason":"inline Contents list, confirmed by preview"}
  ]
}

If nothing meets the criteria, finish with an empty operations list.
""".strip()


def _navigation_user_prompt(
    document: SourceDocument,
    source_overview: dict | None = None,
    previous_phase_summaries: list[dict] | None = None,
) -> str:
    first_blocks = [
        {
            "block_id": b.block_id,
            "line": b.line_start,
            "text": _short_text(b.text),
            "href": b.href,
            "tag": b.tag,
            "classes": b.classes,
            "roles": b.role_candidates,
        }
        for b in document.blocks[:12]
    ]
    summary = {
        "source_type": document.source_type,
        "filename": document.filename,
        "block_count": len(document.blocks),
        "nav_titles_preview": document.nav_titles[:20],
        "source_overview": source_overview or {},
        "first_blocks": first_blocks,
    }
    parts = [
        "Identify and delete TOC/navigation sections from this source. "
        "Inspect the document structure and navigation entries first.\n\n"
        + json.dumps(summary, ensure_ascii=False, indent=2)
    ]
    if previous_phase_summaries:
        parts.append("\nPrevious phases:\n" + json.dumps(previous_phase_summaries, ensure_ascii=False, indent=2))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Phase 3 — Boilerplate / copyright removal
# ---------------------------------------------------------------------------

def _boilerplate_system_prompt(remove_footnotes: bool = False) -> str:
    footnote_line = (
        "\n- Footnotes and endnotes (the user requested footnote removal). "
        "Use find_footnote_candidates to locate them. Preview before deleting."
        if remove_footnotes
        else ""
    )
    footnote_tool = (
        '\n{"action":"find_footnote_candidates","arguments":{"max_candidates":60}}'
        if remove_footnotes
        else ""
    )
    base = (
        "You are preparing extracted book text for TTS/audiobook generation.\n"
        "\n"
        "Your ONLY job: identify and delete front/back matter that should not be read aloud.\n"
        "\n"
        "WHAT TO DELETE:\n"
        "- Copyright notices and publisher rights statements\n"
        "- License text and terms of use (any license, any edition)\n"
        "- Publisher colophon and edition/ISBN pages\n"
        '- "Also by this author" or "Other books in this series" advertisement pages\n'
        "- Publisher catalogue pages and back-of-book advertisements\n"
        "- Author biography pages that are clearly promotional ad copy (not narrative)\n"
        "- Preamble disclaimers about the digital or printed edition of the book\n"
        '- "Produced by", "Digitized by", or "Transcribed by" volunteer credits\n'
        "- Donation solicitations or distribution notices from any digitization project\n"
        "- Title/author/edition headers that appear as isolated duplicate blocks at the very start\n"
        + footnote_line
        + "\n"
        "\n"
        "WHAT NOT TO DELETE:\n"
        "- The book's narrative prose and chapter text\n"
        "- Chapter headings (marked in a later phase)\n"
        "- The title heading on the title page (the publisher colophon below it can be deleted)\n"
        "- Epigraphs and quotations that open chapters\n"
        "- Forewords, prefaces, or introductions that are authored narrative text\n"
        "- Dedications longer than 2–3 lines (treat as narrative)\n"
        "\n"
        "SEARCH STRATEGY — check BOTH ends:\n"
        "1. Inspect the FIRST 2–3 documents / first 40–60 blocks for front-matter boilerplate\n"
        "   (copyright page, preamble, licensing statement, edition notice).\n"
        "2. Inspect the LAST 2–3 documents / last 40–60 blocks for end-matter boilerplate\n"
        "   (colophon, advertisements, digitization credits, donation notices).\n"
        "3. Use analyze_cleanup_structure to find candidate groups anywhere in the document.\n"
        "4. Confirm with preview before deleting.\n"
        "\n"
        "Rules:\n"
        "- Blocks/documents pre-annotated with `deterministic_boilerplate` or `deterministic_footnote` in their roles should be prioritized. Audit them for correctness and delete them if they represent boilerplate/copyright or footnotes/endnotes.\n"
        "- Confirm a section with preview before proposing deletion.\n"
        "- Prefer delete_by_selector (href or class) for complete boilerplate documents.\n"
        "- Use delete_range for inline boilerplate sections in otherwise narrative documents.\n"
        "- Work across languages.\n"
        "\n"
        "You must answer every turn with one JSON object and no prose.\n"
        "\n"
        "Available commands:\n"
        '{"action":"preview","arguments":{"start_line":1,"end_line":40,"max_blocks":20}}\n'
        '{"action":"inspect_document_structure","arguments":{"max_documents":20}}\n'
        '{"action":"preview_selector","arguments":{"selector":{"href":"copyright.xhtml"},"max_blocks":15}}\n'
        '{"action":"analyze_cleanup_structure","arguments":{"max_candidates":15}}\n'
        '{"action":"search","arguments":{"query":"copyright OR rights reserved OR ISBN OR published by OR produced by OR digitized by","mode":"plain","case_sensitive":false,"max_hits":15}}\n'
        '{"action":"regex_search","arguments":{"pattern":"...","flags":"i","max_hits":20}}\n'
        '{"action":"batch","arguments":{"commands":[...]}}'
        + footnote_tool
        + "\n"
        "\n"
        "Finish command — only delete_range, delete_blocks, delete_by_selector are accepted:\n"
        "{\n"
        '  "action": "finish",\n'
        '  "summary": "brief description of what was removed",\n'
        '  "confidence": 0.85,\n'
        '  "operations": [\n'
        '    {"op":"delete_by_selector","selector":{"href":"copyright.xhtml"},"reason":"publisher copyright page, confirmed by preview"},\n'
        '    {"op":"delete_range","start_line":5,"end_line":18,"reason":"preamble/licensing statement, confirmed by preview"}\n'
        "  ]\n"
        "}\n"
        "\n"
        "If nothing meets the criteria, finish with an empty operations list."
    )
    return base.strip()


def _boilerplate_user_prompt(
    document: SourceDocument,
    source_overview: dict | None = None,
    previous_phase_summaries: list[dict] | None = None,
) -> str:
    first_blocks = [
        {
            "block_id": b.block_id,
            "line": b.line_start,
            "text": _short_text(b.text),
            "href": b.href,
            "tag": b.tag,
            "classes": b.classes,
            "roles": b.role_candidates,
        }
        for b in document.blocks[:12]
    ]
    last_blocks = [
        {
            "block_id": b.block_id,
            "line": b.line_start,
            "text": _short_text(b.text),
            "href": b.href,
            "tag": b.tag,
            "classes": b.classes,
        }
        for b in document.blocks[-10:]
    ]
    summary = {
        "source_type": document.source_type,
        "filename": document.filename,
        "block_count": len(document.blocks),
        "source_overview": source_overview or {},
        "first_blocks": first_blocks,
        "last_blocks": last_blocks,
    }
    parts = [
        "Identify and delete boilerplate/copyright content that should not be read aloud. "
        "Check BOTH the first 2–3 documents (copyright, preamble, licensing) AND the last "
        "2–3 documents (colophon, ads, digitization credits). "
        "Use analyze_cleanup_structure for a complete scan, and preview sections before deleting.\n\n"
        + json.dumps(summary, ensure_ascii=False, indent=2)
    ]
    if previous_phase_summaries:
        parts.append("\nPrevious phases:\n" + json.dumps(previous_phase_summaries, ensure_ascii=False, indent=2))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Phase 4 — Repeated element removal
# ---------------------------------------------------------------------------

def _repeated_elements_system_prompt() -> str:
    return """
You are preparing extracted book text for TTS/audiobook generation.

Your ONLY job: identify and delete elements that repeat throughout the document but are not narrative text.

WHAT TO DELETE:
- Running page headers: the book title or author name repeated at the top of each page/section
- Running page footers: similar repeated text at the bottom of each page/section
- Standalone page numbers: blocks containing only a number (or a number with minimal punctuation)
- Image captions and image alt-text blocks (short descriptive text for images, not narrative)
- Repeated section separators that are not meaningful (e.g. "* * *" repeated dozens of times)

WHAT NOT TO DELETE:
- Chapter headings (even if they look like short patterns — they are NOT repeated across every page)
- Narrative text of any kind
- Meaningful ornamental separators between scenes (a single "* * *" between scenes should stay)
- Page numbers that are part of a TOC (already handled in an earlier phase)

Rules:
- Use list_repeated_lines first to discover repeated patterns.
- Confirm a pattern is non-narrative with preview (check the surrounding context).
- If nothing meets the criteria, finish with an empty operations list.
- Prefer delete_by_selector with text_regex for page numbers.
- Use delete_blocks with the block_ids list for running headers/footers.

You must answer every turn with one JSON object and no prose.

Available commands:
{"action":"list_repeated_lines","arguments":{"min_repeats":3,"max_length":120}}
{"action":"preview","arguments":{"around_hit_id":"hit:1","before":3,"after":3}}
{"action":"preview","arguments":{"start_line":1,"end_line":40,"max_blocks":15}}
{"action":"search","arguments":{"query":"text","mode":"plain","max_hits":20}}
{"action":"regex_search","arguments":{"pattern":"^\\d+$","flags":"m","max_hits":30}}
{"action":"batch","arguments":{"commands":[...]}}

Finish command — only delete_range, delete_blocks, delete_by_selector are accepted:
{
  "action": "finish",
  "summary": "brief description of repeated elements found and deleted",
  "confidence": 0.85,
  "operations": [
    {"op":"delete_blocks","block_ids":["b:1","b:42","b:87"],"reason":"running page headers — book title repeated on every page, confirmed by preview"},
    {"op":"delete_by_selector","selector":{"text_regex":"^\\d{1,4}$"},"reason":"standalone page numbers"}
  ]
}

If nothing meets the criteria, finish with an empty operations list.
""".strip()


def _repeated_elements_user_prompt(
    document: SourceDocument,
    previous_phase_summaries: list[dict] | None = None,
) -> str:
    summary = {
        "source_type": document.source_type,
        "filename": document.filename,
        "block_count": len(document.blocks),
        "warnings": [_short_text(w) for w in document.warnings[:10]],
    }
    parts = [
        "Identify and delete repeated non-narrative elements (running headers/footers, "
        "page numbers, image captions). Start with list_repeated_lines.\n\n"
        + json.dumps(summary, ensure_ascii=False, indent=2)
    ]
    if previous_phase_summaries:
        parts.append("\nPrevious phases:\n" + json.dumps(previous_phase_summaries, ensure_ascii=False, indent=2))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Phase 5 — Chapter marking
# ---------------------------------------------------------------------------

def _chapter_marking_system_prompt() -> str:
    return """
You are preparing extracted book text for TTS/audiobook generation.
TOC entries, copyright text, and repeated non-narrative elements have already been removed.

Your ONLY job: identify and mark all narrative chapter and section headings with [[Chapter]] markers.

WHAT TO MARK:
- Chapter headings in any form: "Chapter 1", "CHAPTER ONE", "I", "Chapter the First", "第一章", etc.
- Part / Volume / Book / Act / Stave headings: "PART ONE", "Volume II", "ACT THREE", "Stave I"
- Named section headings that divide the narrative: "THE STORM", "In Which Our Hero..."
- Prologues, Epilogues, Afterwords that are part of the narrative structure
- ALL headings in ANY language — do not assume English-only patterns

WHAT NOT TO MARK:
- Epigraphs or quotations at chapter openings (they are not headings)
- Subheadings within a chapter that are not genuine narrative divisions
- The overall book title itself
- Blank lines or ornamental separators

Rules:
- Blocks pre-annotated with `deterministic_chapter` are scheduled markers, not ground truth. Audit them; retain real narrative divisions and use unmark_chapter to reject an incorrect scheduled marker.
- Use analyze_chapter_structure first to understand the heading pattern.
- Before finishing, verify your proposed operations cover all likely chapters.
- Use mark_chapters_by_selector when a selector cleanly covers the complete heading set without false positives.
- Use mark_chapter for individual headings that do not fit a pattern.
- Preview a selector before using it — confirm it matches ONLY narrative headings.
- Prefer completeness: it is better to mark all headings than to miss some.
- Work across languages.

You must answer every turn with one JSON object and no prose.

Available commands:
{"action":"analyze_chapter_structure","arguments":{"max_candidates":80}}
{"action":"find_heading_candidates","arguments":{"max_candidates":100}}
{"action":"preview_selector","arguments":{"selector":{"role":"heading","text_regex":"^CHAPTER"},"max_blocks":20}}
{"action":"preview","arguments":{"start_line":1,"end_line":50,"max_blocks":20}}
{"action":"inspect_navigation","arguments":{"max_entries":80}}
{"action":"search","arguments":{"query":"chapter OR part OR volume OR act","mode":"plain","case_sensitive":false,"max_hits":30}}
{"action":"regex_search","arguments":{"pattern":"...","flags":"i","max_hits":30}}
{"action":"evaluate_operations","arguments":{"operations":[...]}}
{"action":"batch","arguments":{"commands":[...]}}

Finish command — only mark_chapter, unmark_chapter, and mark_chapters_by_selector are accepted:
{
  "action": "finish",
  "summary": "brief explanation of heading pattern and marking strategy",
  "confidence": 0.9,
  "operations": [
    {"op":"mark_chapters_by_selector","selector":{"role":"heading","text_regex":"^CHAPTER\\s+"},"reason":"complete narrative chapter pattern, confirmed by preview_selector"},
    {"op":"mark_chapter","block_id":"b:5","title":"Prologue"},
    {"op":"unmark_chapter","block_id":"b:9","reason":"running header, not a narrative division"}
  ]
}
""".strip()


def _chapter_marking_user_prompt(
    document: SourceDocument,
    source_overview: dict | None = None,
    previous_phase_summaries: list[dict] | None = None,
) -> str:
    first_blocks = [
        {
            "block_id": b.block_id,
            "line": b.line_start,
            "text": _short_text(b.text),
            "href": b.href,
            "tag": b.tag,
            "classes": b.classes,
            "roles": b.role_candidates,
        }
        for b in document.blocks[:12]
    ]
    last_blocks = [
        {
            "block_id": b.block_id,
            "line": b.line_start,
            "text": _short_text(b.text),
            "href": b.href,
            "tag": b.tag,
            "classes": b.classes,
            "roles": b.role_candidates,
        }
        for b in document.blocks[-8:]
    ]
    summary = {
        "source_type": document.source_type,
        "filename": document.filename,
        "language_hint": document.language,
        "block_count": len(document.blocks),
        "nav_titles_preview": document.nav_titles[:20],
        "source_overview": source_overview or {},
        "first_blocks": first_blocks,
        "last_blocks": last_blocks,
    }
    parts = [
        "Mark all narrative chapter and section headings in this cleaned source. "
        "Start with analyze_chapter_structure, then verify completeness before finishing.\n\n"
        + json.dumps(summary, ensure_ascii=False, indent=2)
    ]
    if previous_phase_summaries:
        parts.append("\nPrevious phases:\n" + json.dumps(previous_phase_summaries, ensure_ascii=False, indent=2))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Legacy single-pass ("full") prompt — kept for backward compatibility
# ---------------------------------------------------------------------------

def _full_system_prompt(remove_footnotes: bool = False) -> str:
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
    {{"op":"mark_chapters_by_selector","selector":{{"role":"heading","text_regex":"^CHAPTER\\\\s+[IVXLCDM]+\\\\."}},\"reason":"complete narrative chapter pattern, confirmed by preview_selector"}},
    {{"op":"replace_range","start_line":10,"end_line":10,"replacement":"small OCR fix","reason":"small OCR fix"}}
  ]
}}
""".strip()


def _full_user_prompt(
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


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

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
