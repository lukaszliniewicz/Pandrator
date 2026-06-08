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
        
        # Detect and strip inline TOC lists (e.g. at start of content files)
        import re
        TOC_HEADER_RE = re.compile(
            r"^(?:tableofcontents|contents|spistre\u015bci|spistresci|inhoud|inhaltsverzeichnis|"
            r"tabledesmati\u00e8res|tabledesmatieres|inhoudsopgave|sommaire|"
            r"\u043e\u0433\u043b\u0430\u0432\u043b\u0435\u043d\u0438\u0435|\u0441\u043e\u0434\u0435\u0440\u0436\u0430\u043d\u0438\u0435|\u0935\u093f\u0937\u092f-\u0938\u0942\u091a\u0940)$",
            re.IGNORECASE
        )
        
        toc_start_idx = None
        in_inline_toc = False
        last_heading_idx = None
        to_remove = set()
        
        for idx, block in enumerate(blocks):
            text = block.get("text", "").strip()
            if not text:
                continue
                
            is_ch = chapters.is_chapter_block(block, idx, lang=detected_lang)
            no_space_text = re.sub(r"\s+", "", text)
            
            if not in_inline_toc and TOC_HEADER_RE.match(no_space_text):
                in_inline_toc = True
                toc_start_idx = idx
                continue
                
            if in_inline_toc:
                if is_ch:
                    last_heading_idx = idx
                elif len(text) > 80:
                    in_inline_toc = False
                    if last_heading_idx is not None:
                        for r_idx in range(toc_start_idx, last_heading_idx):
                            to_remove.add(r_idx)
                    else:
                        for r_idx in range(toc_start_idx, idx):
                            to_remove.add(r_idx)
                            
        if to_remove:
            blocks = [b for idx, b in enumerate(blocks) if idx not in to_remove]
            
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
        last_chapter_title_norm = ""
        
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
            last_chapter_title_norm = re.sub(r"\s+", "", recovered_title).lower()
            
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
            
            if is_ch:
                title = block.get("text", "").strip()
                if matched_title:
                    title = matched_title
                if title:
                    if not title.startswith("[[Chapter]]"):
                        block_copy["text"] = f"[[Chapter]]{title}"
                    last_chapter_title_norm = re.sub(r"\s+", "", title).lower()
            elif matched_title:
                # TOC matches a non-heading block (e.g. a paragraph).
                # To prevent content loss, do NOT replace the paragraph's text.
                # Instead, insert a new heading block before it, but only if it's not a duplicate.
                matched_title_norm = re.sub(r"\s+", "", matched_title).lower()
                if matched_title_norm != last_chapter_title_norm:
                    new_heading_title = matched_title
                    if not new_heading_title.startswith("[[Chapter]]"):
                        new_heading_title = f"[[Chapter]]{new_heading_title}"
                    heading_block = {
                        "tag": "h1",
                        "id": "",
                        "classes": ["chapter"],
                        "parts": [{"type": "text", "content": matched_title}],
                        "text": new_heading_title,
                        "href": doc_href,
                        "block_index": -1
                    }
                    formatted_blocks.append(heading_block)
                    last_chapter_title_norm = matched_title_norm
                        
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
            
    full_text = "\n\n".join(extracted_chapters)
    
    # Strip Project Gutenberg boilerplate automatically if start/end markers are present
    import re
    start_pattern = re.compile(r"\*\*\*\s*START OF (?:THE|THIS) PROJECT GUTENBERG EBOOK.*?\*\*\*", re.IGNORECASE)
    end_pattern = re.compile(r"\*\*\*\s*END OF (?:THE|THIS) PROJECT GUTENBERG EBOOK.*?\*\*\*", re.IGNORECASE)
    
    start_match = start_pattern.search(full_text)
    if start_match:
        full_text = full_text[start_match.end():].strip()
        
    end_match = end_pattern.search(full_text)
    if end_match:
        full_text = full_text[:end_match.start()].strip()
        
    return full_text
