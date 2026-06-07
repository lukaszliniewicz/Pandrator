from __future__ import annotations

import os

def is_toc_file(href: str, parsed_doc: dict, spine: list[dict]) -> bool:
    """
    Determines if a spine document is a Table of Contents (TOC) or Navigation page.
    Uses filename checks, link density heuristics, and link count ceilings.
    """
    name_lower = href.lower()
    
    # 1. Explicit filename checks
    if any(x in name_lower for x in ["toc", "contents", "nav"]):
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
    spine_hrefs = {item["href"].lower() for item in spine}
    num_spine_links = 0
    for a in anchors:
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
            
    # 3. Apply Heuristics
    # High link count ceiling (large chapter summaries can have low density but high link count)
    if num_spine_links > 8:
        return True
        
    # High link density relative to text length
    if num_spine_links > 4 and (num_spine_links / max(1, word_count)) > 0.08:
        return True
        
    return False
