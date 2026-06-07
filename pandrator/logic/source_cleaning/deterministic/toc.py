from __future__ import annotations

import os

def is_toc_file(href: str, parsed_doc: dict, spine: list[dict]) -> bool:
    """
    Determines if a spine document is a Table of Contents (TOC) or Navigation page.
    Uses filename checks, link density heuristics, and link count ceilings.
    """
    name_lower = href.lower()
    
    # 1. Explicit filename checks (word-tokenized to prevent false positives like 'anavenger' or 'apinkstocking')
    import re
    base_name = os.path.splitext(os.path.basename(name_lower))[0]
    clean_name = re.sub(r'[^a-z]', ' ', base_name)
    words = clean_name.split()
    if any(w in ['toc', 'contents', 'nav', 'navigation'] for w in words):
        return True
        
    # Gather anchors in this document and calculate word metrics
    blocks = parsed_doc.get("blocks", [])
    anchors = []
    total_words = 0
    link_words = 0
    
    from .footnotes import is_footnote_ref
    spine_hrefs = {item["href"].lower() for item in spine}
    
    for block in blocks:
        text = block.get("text", "")
        words = text.split()
        total_words += len(words)
        
        for part in block.get("parts", []):
            if part.get("type") == "anchor":
                anchors.append(part)
                if not is_footnote_ref(part):
                    content = part.get("content", "")
                    link_words += len(content.split())
                    
    link_word_ratio = link_words / max(1, total_words)
                
    # 2. Count internal hyperlinks pointing to other spine files
    num_spine_links = 0
    unique_targets = set()
    for a in anchors:
        if is_footnote_ref(a):
            continue
        h = a.get("href", "")
        if not h:
            continue
        # Extract target file path (remove fragment identifier)
        target_file = h.split("#")[0].strip()
        # If fragment only (e.g. href="#chapter1"), it points to the current file
        if not target_file:
            continue
        # Resolve target path relative to current document's directory in the archive
        curr_dir = os.path.dirname(href)
        resolved_target = os.path.normpath(os.path.join(curr_dir, target_file)).replace("\\", "/")
        target_lower = resolved_target.lower()
        # Check if the target is in the spine and is not the current file itself
        if target_lower in spine_hrefs and target_lower != name_lower:
            num_spine_links += 1
            unique_targets.add(target_lower)
            
    # 3. Apply Heuristics
    num_unique_targets = len(unique_targets)
    
    # Enforce link word ratio check to prevent regular chapters with cross-references from being classified as TOC
    if link_word_ratio < 0.3:
        return False
        
    # High unique target files count (real TOC links to many files)
    if num_unique_targets > 8:
        return True
        
    # High unique target files count with high density of links pointing to them
    if num_unique_targets > 4 and (num_spine_links / max(1, total_words)) > 0.08:
        return True
        
    return False

def build_global_toc_map(structure: dict) -> dict[str, str]:
    """
    Builds a global TOC map from the parsed structure.
    Maps:
      - filename (lowercase, e.g. 'text/chap1.html') -> title
      - filename#fragment (lowercase, e.g. 'text/chap1.html#sec1') -> title
    """
    toc_map = {}
    spine = structure.get("spine", [])
    parsed_docs = structure.get("parsed_documents", {})
    
    # 1. Incorporate NCX TOC mapping if available
    ncx_toc = structure.get("ncx_toc", {})
    for src, title in ncx_toc.items():
        src_lower = src.lower().replace("\\", "/").strip()
        if src_lower and title:
            toc_map[src_lower] = title
            if "#" in src_lower:
                base = src_lower.split("#")[0]
                if base not in toc_map:
                    toc_map[base] = title

    # 2. Extract anchors from all HTML files in spine classified as TOC
    for idx, item in enumerate(spine):
        href = item["href"]
        if href not in parsed_docs:
            continue
        doc = parsed_docs[href]
        
        # Check if classified as TOC
        if is_toc_file(href, doc, spine):
            blocks = doc.get("blocks", [])
            for block in blocks:
                for part in block.get("parts", []):
                    if part.get("type") == "anchor":
                        h = part.get("href", "")
                        title = part.get("content", "").strip()
                        if h and title:
                            # Resolve path relative to the TOC file's directory
                            target_file = h.split("#")[0].strip()
                            frag = h.split("#")[1].strip() if "#" in h else ""
                            
                            curr_dir = os.path.dirname(href)
                            resolved_file = os.path.normpath(os.path.join(curr_dir, target_file)).replace("\\", "/")
                            resolved_href = resolved_file.lower()
                            if frag:
                                resolved_href += f"#{frag.lower()}"
                                
                            toc_map[resolved_href] = title
                            base_lower = resolved_file.lower()
                            if base_lower not in toc_map:
                                toc_map[base_lower] = title
                                
    return toc_map
