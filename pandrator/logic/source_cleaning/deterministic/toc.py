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
        
    # Gather anchors in this document
    blocks = parsed_doc.get("blocks", [])
    anchors = []
    word_count = 0
    
    for block in blocks:
        word_count += len(block.get("text", "").split())
        for part in block.get("parts", []):
            if part.get("type") == "anchor":
                anchors.append(part)
                
    # 2. Count internal hyperlinks pointing to other spine files
    from .footnotes import is_footnote_ref
    spine_hrefs = {item["href"].lower() for item in spine}
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
        # Check if the target is in the spine and is not the current file itself
        target_lower = target_file.lower()
        if target_lower in spine_hrefs and target_lower != name_lower:
            num_spine_links += 1
            unique_targets.add(target_lower)
            
    # 3. Apply Heuristics
    num_unique_targets = len(unique_targets)
    
    # High unique target files count (real TOC links to many files)
    if num_unique_targets > 8:
        return True
        
    # High unique target files count with high density of links pointing to them
    if num_unique_targets > 4 and (num_spine_links / max(1, word_count)) > 0.08:
        return True
        
    return False
