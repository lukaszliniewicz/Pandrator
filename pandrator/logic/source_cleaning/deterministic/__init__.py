from __future__ import annotations

from . import parser
from . import toc
from . import boilerplate
from . import chapters
from . import footnotes

def extract_clean_epub(epub_path: str, remove_footnotes: bool = False) -> str:
    """
    Extracts, filters, and formats text from an EPUB file.
    Performs deterministic removal of TOCs, front/end boilerplate,
    multilingual chapter heading markings, and inline footnote repositioning.
    """
    structure = parser.unpack_epub_structure(epub_path)
    spine = structure["spine"]
    parsed_docs = structure["parsed_documents"]
    
    # 1. Classify spine documents to filter out TOC and boilerplate
    total_spine_files = len(spine)
    content_docs = []
    
    for idx, item in enumerate(spine):
        href = item["href"]
        if href not in parsed_docs:
            continue
            
        doc = parsed_docs[href]
        size = doc["size"]
        
        # Heuristics checks
        if toc.is_toc_file(href, doc, spine):
            continue
        if boilerplate.is_front_boilerplate(idx, total_spine_files, size, href):
            continue
        if boilerplate.is_end_boilerplate(idx, total_spine_files, href):
            continue
            
        content_docs.append(href)
        
    # 2. Extract and format text from content documents
    repositioned_block_ids = set()  # set of (doc_href, block_index)
    extracted_chapters = []
    
    # Pre-build backlink map once for the entire book to optimize search complexity to O(1)
    backlink_map = footnotes.build_backlink_map(parsed_docs)
    
    for doc_href in content_docs:
        doc = parsed_docs[doc_href]
        blocks = doc["blocks"]
        
        # Format chapter headings
        formatted_blocks = []
        for idx_in_doc, block in enumerate(blocks):
            block_copy = block.copy()
            if chapters.is_chapter_block(block, idx_in_doc):
                title = block.get("text", "").strip()
                if title:
                    # Mark as chapter
                    if not title.startswith("[[Chapter]]"):
                        block_copy["text"] = f"[[Chapter]]{title}"
            formatted_blocks.append(block_copy)
            
        # Process and reposition footnotes
        doc_lines = footnotes.reposition_footnotes_in_document(
            doc_href,
            formatted_blocks,
            parsed_docs,
            repositioned_block_ids,
            remove_footnotes=remove_footnotes,
            backlink_map=backlink_map
        )
        
        chapter_text = "\n\n".join(doc_lines).strip()
        if chapter_text:
            extracted_chapters.append(chapter_text)
            
    return "\n\n".join(extracted_chapters)
