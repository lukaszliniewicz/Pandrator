from __future__ import annotations

import os
import re
import collections

def is_footnote_ref(anchor: dict) -> bool:
    """
    Checks if an anchor block part is a footnote reference.
    """
    href = anchor.get("href", "")
    if not href or "#" not in href:
        return False
    frag = href.split("#")[-1].lower()
    cls = (anchor.get("class") or "").lower()
    id_val = (anchor.get("id") or "").lower()
    epub_type = (anchor.get("epub_type") or "").lower()
    
    # Standard EPUB3 indicators
    if "noteref" in epub_type or "footnote" in epub_type:
        return True
        
    # Naming convention heuristics
    keywords = ["fn", "footnote", "note", "ref"]
    
    # 1. Check if the target file name itself points to a notes file (e.g. notes.htm) and has a number in fragment
    target_file = os.path.basename(href.split("#")[0]).lower()
    if any(k in target_file for k in ["note", "fn"]) and re.search(r"\d+", frag):
        return True
        
    # 2. Match standard note/footnote patterns in fragment or ID (supporting fn, en, nr, ref followed by digits)
    if re.search(r"(?:^|_)(?:fn|en|nr|ref)\d+|footnote|(?<!\w)note\d+", frag):
        return True
    if re.search(r"(?:^|_)(?:fn|en|nr|ref)\d+|footnote|(?<!\w)note\d+", id_val):
        return True
        
    # 3. Class/ID keyword matching
    if any(k in cls for k in keywords):
        return True
    if any(k in id_val for k in keywords):
        return True
    return False

def clean_footnote_text(text: str) -> str:
    """
    Cleans leading markers (e.g. [1], 1., *, etc.) from footnote text for natural TTS flow.
    """
    cleaned = text.strip()
    cleaned = re.sub(r'^(?:\[(?:fn)?\d+\]|\d+[\.)]|[*†‡])\s*', '', cleaned)
    return cleaned.strip()

def split_sentences_regex(text: str) -> list[str]:
    """
    Fast sentence splitter that handles common abbreviations without look-behind errors.
    """
    pattern = re.compile(r'([.!?])\s+(?=[A-Z\u0100-\u017f\u0400-\u04ff\u4e00-\u9fff])')
    abbreviations = {
        "mr", "st", "dr", "mrs", "ms", "jr", "sr", "prof", "gen", "capt", "col", "lt", 
        "m", "mlle", "mme", "vs", "ca", "eg", "ie", "etc", "rozdzia", "rozdz"
    }
    
    sentences = []
    last_idx = 0
    
    for match in pattern.finditer(text):
        split_idx = match.start(1) + 1
        prev_text = text[max(0, split_idx-15):split_idx-1]
        words = re.findall(r'\b\w+\b$', prev_text)
        if words:
            prev_word = words[0].lower()
            if prev_word in abbreviations:
                continue
                
        sentences.append(text[last_idx:match.end()].strip())
        last_idx = match.end()
        
    if last_idx < len(text):
        sentences.append(text[last_idx:].strip())
        
    return [s for s in sentences if s]

def build_backlink_map(parsed_documents: dict) -> dict[tuple[str, str], dict]:
    """
    Builds a pre-indexed backlink map for the entire EPUB.
    Maps (referencing_doc_basename, number_string) -> target_block (footnote text block)
    """
    backlink_map = {}
    for doc_href, doc in parsed_documents.items():
        for block in doc.get("blocks", []):
            for part in block.get("parts", []):
                if part.get("type") == "anchor":
                    href = part.get("href", "")
                    if "#" in href:
                        target_file, target_frag = href.split("#", 1)
                        target_file_basename = os.path.basename(target_file).lower()
                        target_frag_lower = target_frag.lower()
                        
                        num_match = re.search(r"\d+", target_frag_lower)
                        if num_match:
                            num = num_match.group(0)
                            if "fnanchor" in target_frag_lower or any(kw in target_frag_lower for kw in ["ref", "anchor", "back"]):
                                target_basename = target_file_basename if target_file_basename else os.path.basename(doc_href).lower()
                                backlink_map[(target_basename, num)] = block
    return backlink_map

def find_gutenberg_fallback_footnote(referencing_href: str, frag_id: str, backlink_map: dict) -> dict | None:
    """
    O(1) fallback lookup using the pre-built backlink map.
    """
    num_match = re.search(r"\d+", frag_id)
    if not num_match:
        return None
    num = num_match.group(0)
    ref_basename = os.path.basename(referencing_href).lower()
    return backlink_map.get((ref_basename, num))

def reposition_footnotes_in_document(
    doc_href: str,
    blocks: list[dict],
    parsed_documents: dict,
    repositioned_block_ids: set[tuple[str, int]],
    remove_footnotes: bool = False,
    backlink_map: dict | None = None
) -> list[str]:
    """
    Repositions footnotes in a document inline, immediately following their marker sentences.
    Modifies repositioned_block_ids to track which blocks have been consumed.
    If remove_footnotes is True, resolves and consumes footnotes without injecting them.
    """
    cleaned_lines = []
    
    for block in blocks:
        block_idx = block["block_index"]
        if (doc_href, block_idx) in repositioned_block_ids:
            continue
            
        parts = block.get("parts", [])
        anchors = [p for p in parts if p["type"] == "anchor" and is_footnote_ref(p)]
        
        if not anchors:
            cleaned_lines.append(block["text"])
            continue
            
        plain_text = block["text"]
        
        # Resolve target footnote blocks
        resolved_footnotes = []
        for anchor in anchors:
            href = anchor.get("href", "")
            target_file_rel, frag_id = href.split("#", 1) if "#" in href else ("", "")
            
            if not target_file_rel:
                target_href = doc_href
            else:
                target_href = target_file_rel
                
            matched_doc_key = None
            for key in parsed_documents:
                if os.path.basename(key).lower() == os.path.basename(target_href).lower():
                    matched_doc_key = key
                    break
                    
            target_block = None
            if matched_doc_key:
                target_doc = parsed_documents[matched_doc_key]
                if frag_id in target_doc.get("ids", {}):
                    block_idx_target = target_doc["ids"][frag_id]
                    target_block = target_doc["blocks"][block_idx_target]
                    repositioned_block_ids.add((matched_doc_key, block_idx_target))
                else:
                    if backlink_map is None:
                        backlink_map = build_backlink_map(parsed_documents)
                    fallback = find_gutenberg_fallback_footnote(doc_href, frag_id, backlink_map)
                    if fallback:
                        target_block = fallback
                        repositioned_block_ids.add((fallback["href"], fallback["block_index"]))
                        
            if target_block:
                fn_text = clean_footnote_text(target_block["text"])
                if fn_text and not remove_footnotes:
                    resolved_footnotes.append((anchor, fn_text))
                    
        if not resolved_footnotes:
            cleaned_lines.append(plain_text)
            continue
            
        # Split block text into sentences and map footnote text blocks to them
        sentences = split_sentences_regex(plain_text)
        sentence_footnotes = collections.defaultdict(list)
        
        search_start = 0
        for anchor, fn_text in resolved_footnotes:
            anchor_content = anchor.get("content", "").strip()
            if not anchor_content:
                sentence_footnotes[len(sentences) - 1].append(fn_text)
                continue
                
            pos = plain_text.find(anchor_content, search_start)
            if pos == -1:
                clean_content = re.sub(r'\D+', '', anchor_content)
                if clean_content:
                    pos = plain_text.find(clean_content, search_start)
                    
            if pos == -1:
                sentence_footnotes[len(sentences) - 1].append(fn_text)
                continue
                
            search_start = pos + len(anchor_content)
            
            # Find the matching sentence index
            curr_len = 0
            found_idx = len(sentences) - 1
            for s_idx, s in enumerate(sentences):
                s_pos = plain_text.find(s, curr_len)
                if s_pos != -1:
                    s_end = s_pos + len(s)
                    if s_pos <= pos <= s_end:
                        found_idx = s_idx
                        curr_len = s_end
                        break
                    curr_len = s_end
            
            sentence_footnotes[found_idx].append(fn_text)
            
        # Rebuild the sentences with inline footnotes
        final_sentences = []
        for s_idx, s in enumerate(sentences):
            final_sentences.append(s)
            if s_idx in sentence_footnotes:
                for fn in sentence_footnotes[s_idx]:
                    final_sentences.append(f" [Footnote: {fn}]")
                    
        cleaned_lines.append("".join(final_sentences))
        
    return cleaned_lines

def is_footnote_file(href: str, size: int) -> bool:
    """
    Determines if a file in the spine is a dedicated footnote/endnote container file.
    """
    name_lower = os.path.basename(href).lower()
    # Matches _fn01, -fn, _footnote, _endnote, etc.
    if re.search(r'[_.-](?:fn|footnote|endnote)(?:\d+)?\b', name_lower):
        return True
    base_name = os.path.splitext(name_lower)[0]
    if base_name in ('fn', 'footnote', 'notes', 'endnotes') or re.match(r'^(?:fn|footnote|notes|endnotes)\d+$', base_name):
        return True
    # For _notes, only match if size is small (< 15000 bytes) or if it starts with 'notes'
    if re.search(r'[_.-]notes(?:\d+)?\b', name_lower) and size < 15000:
        return True
    return False
