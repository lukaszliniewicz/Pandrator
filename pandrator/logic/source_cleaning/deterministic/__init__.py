from __future__ import annotations

from . import parser
from . import toc
from . import boilerplate
from . import chapters
from . import footnotes

def extract_clean_epub(epub_path: str, remove_footnotes: bool = False, filter_citations: bool = True) -> str:
    """
    Extracts, filters, and formats text from an EPUB file.
    Performs deterministic removal of TOCs, front/end boilerplate,
    multilingual chapter heading markings, and inline footnote repositioning.
    """
    structure = parser.unpack_epub_structure(epub_path)
    spine = structure["spine"]
    parsed_docs = structure["parsed_documents"]
    
    # Detect language of the book
    detected_lang = footnotes.detect_book_language(structure.get("metadata", {}), parsed_docs)
    
    # 1. Classify spine documents to filter out TOC, boilerplate, and footnote files
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
        if footnotes.is_footnote_file(href, size):
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
    
    # Build global TOC map
    global_toc = toc.build_global_toc_map(structure)
    
    for doc_href in content_docs:
        doc = parsed_docs[doc_href]
        blocks = doc["blocks"]
        
        # Check if this document has any headings detected
        has_headings = False
        for idx_in_doc, block in enumerate(blocks):
            if chapters.is_chapter_block(block, idx_in_doc, lang=detected_lang):
                has_headings = True
                break
                
        # Check if any block matches a fragment in the global TOC
        has_fragment_match = False
        if not has_headings:
            for idx_in_doc, block in enumerate(blocks):
                block_ids = []
                if block.get("id"):
                    block_ids.append(block["id"])
                for nid, b_idx in doc.get("ids", {}).items():
                    if b_idx == idx_in_doc:
                        block_ids.append(nid)
                for bid in block_ids:
                    frag_key = f"{doc_href.lower()}#{bid.lower()}"
                    if frag_key in global_toc:
                        has_fragment_match = True
                        break
                if has_fragment_match:
                    break
                    
        # Recover chapter heading from global TOC if 0 headings and 0 fragment matches are detected in this file
        recovered_title = None
        if not has_headings and not has_fragment_match:
            recovered_title = global_toc.get(doc_href.lower())
            
        # Format chapter headings
        formatted_blocks = []
        
        # Inject recovered title block if found
        if not has_headings and recovered_title:
            heading_block = {
                "tag": "h1",
                "id": "",
                "classes": ["chapter"],
                "parts": [{"type": "text", "content": recovered_title}],
                "text": f"[[Chapter]]{recovered_title}",
                "href": doc_href,
                "block_index": -1
            }
            formatted_blocks.append(heading_block)
            
        for idx_in_doc, block in enumerate(blocks):
            block_copy = block.copy()
            
            # Check if this block matches a fragment link in the global TOC
            matched_title = None
            block_ids = []
            if block.get("id"):
                block_ids.append(block["id"])
            for nid, b_idx in doc.get("ids", {}).items():
                if b_idx == idx_in_doc:
                    block_ids.append(nid)
                    
            for bid in block_ids:
                frag_key = f"{doc_href.lower()}#{bid.lower()}"
                if frag_key in global_toc:
                    matched_title = global_toc[frag_key]
                    break
                    
            is_ch = chapters.is_chapter_block(block, idx_in_doc, lang=detected_lang)
            
            if matched_title:
                if not matched_title.startswith("[[Chapter]]"):
                    block_copy["text"] = f"[[Chapter]]{matched_title}"
            elif is_ch:
                title = block.get("text", "").strip()
                if title:
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
            filter_citations=filter_citations,
            detected_lang=detected_lang,
            backlink_map=backlink_map
        )
        
        chapter_text = "\n\n".join(doc_lines).strip()
        if chapter_text:
            extracted_chapters.append(chapter_text)
            
    return "\n\n".join(extracted_chapters)
