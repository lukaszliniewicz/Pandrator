from __future__ import annotations

import os
import re
import zipfile
import urllib.parse
import collections
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

# Helper to find an XML element in a namespace-agnostic way
def find_elem(element, tag_name):
    if element is None:
        return None
    curr_tag = element.tag.split("}")[-1]
    if curr_tag == tag_name:
        return element
    for child in element:
        res = find_elem(child, tag_name)
        if res is not None:
            return res
    return None

# Helper to find all XML elements in a namespace-agnostic way
def find_all_elems(element, tag_name):
    results = []
    if element is None:
        return results
    def recurse(el):
        curr_tag = el.tag.split("}")[-1]
        if curr_tag == tag_name:
            results.append(el)
        for child in el:
            recurse(child)
    recurse(element)
    return results

class EPUBHTMLParser(HTMLParser):
    def __init__(self, href: str):
        super().__init__()
        self.href = href
        self.blocks = []  # list of dicts (Block models)
        self.ids = {}  # map id -> block_index
        self.classes = collections.defaultdict(int)
        
        # Tag stack to track parents: (tag_name, class_val, id_val)
        self.tag_stack = []
        
        # Current block accumulator
        self.current_block = None
        self.current_block_tag = None
        
        # Accumulating plain text vs anchor elements inside block
        self.current_parts = []
        
        # Track anchor tags stack to support nested tags inside anchor
        self.anchor_stack = []
        self.current_block_nested_ids = []

    def handle_starttag(self, tag, attrs):
        attr_dict = dict(attrs)
        id_val = attr_dict.get("id", "")
        class_val = attr_dict.get("class", "")
        href_val = attr_dict.get("href", "")
        epub_type = attr_dict.get("epub:type", "")
        
        self.tag_stack.append((tag, class_val, id_val))
        
        if class_val:
            for c in class_val.split():
                self.classes[c] += 1
                
        # Block-level tag start
        block_tags = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "dd", "dt", "blockquote", "aside", "section", "article"}
        if tag in block_tags:
            # If there's an existing block, close it
            self._close_current_block()
            
            self.current_block_tag = tag
            self.current_block_id = id_val
            self.current_block_classes = [c for c in class_val.split() if c] if class_val else []
            self.current_parts = []
            self.current_block_nested_ids = []
            
        # Anchor tag start
        if tag == "a" and href_val:
            # We are entering an anchor
            anchor_info = {
                "href": href_val,
                "id": id_val,
                "class": class_val,
                "epub_type": epub_type,
                "accumulator": []
            }
            self.anchor_stack.append(anchor_info)
            
        # If an ID is defined and we have a block, map the ID
        if id_val and self.current_block_tag is not None:
            self.current_block_nested_ids.append(id_val)

    def handle_data(self, data):
        if self.current_block_tag is not None:
            if self.anchor_stack:
                # Accumulate text inside the active anchor
                self.anchor_stack[-1]["accumulator"].append(data)
            else:
                # Normal text outside anchors
                if self.current_parts and self.current_parts[-1]["type"] == "text":
                    self.current_parts[-1]["content"] += data
                else:
                    self.current_parts.append({
                        "type": "text",
                        "content": data
                    })

    def handle_endtag(self, tag):
        # Anchor tag end
        if tag == "a" and self.anchor_stack:
            anchor_info = self.anchor_stack.pop()
            anchor_content = "".join(anchor_info["accumulator"])
            
            # Add the anchor as a part
            self.current_parts.append({
                "type": "anchor",
                "href": anchor_info["href"],
                "id": anchor_info["id"],
                "class": anchor_info["class"],
                "epub_type": anchor_info["epub_type"],
                "content": anchor_content
            })
            
        # Block tag end
        if self.current_block_tag == tag:
            self._close_current_block()
            
        if self.tag_stack:
            self.tag_stack.pop()

    def _close_current_block(self):
        if self.current_block_tag is None:
            return
            
        # Normalize and construct plain text of the block
        parts_clean = []
        for p in self.current_parts:
            if p["type"] == "text":
                content = p["content"]
                if content:
                    parts_clean.append(p)
            else:
                parts_clean.append(p)
                
        # Build plain text representation
        plain_text = "".join(p["content"] for p in parts_clean).strip()
        
        # Only save block if it contains actual content or has anchors/ids
        if plain_text or self.current_block_id or self.current_block_nested_ids or any(p["type"] == "anchor" for p in parts_clean):
            block_idx = len(self.blocks)
            block = {
                "tag": self.current_block_tag,
                "id": self.current_block_id,
                "classes": self.current_block_classes,
                "parts": parts_clean,
                "text": plain_text,
                "href": self.href,
                "block_index": block_idx
            }
            self.blocks.append(block)
            
            # Map ID to block index
            if self.current_block_id:
                self.ids[self.current_block_id] = block_idx
            for nid in self.current_block_nested_ids:
                self.ids[nid] = block_idx
                
        # Reset current block state
        self.current_block_tag = None
        self.current_parts = []

    def close(self):
        # Force closing any open blocks
        self._close_current_block()
        super().close()

def parse_single_document(z: zipfile.ZipFile, opf_dir: str, rel_path: str) -> dict:
    """Parses a single HTML document from the EPUB zip and returns its structural dict."""
    zip_path = os.path.normpath(os.path.join(opf_dir, rel_path)).replace("\\", "/")
    
    # Try case-insensitive zip file name lookup
    zip_match = None
    for name in z.namelist():
        if name.lower() == zip_path.lower():
            zip_match = name
            break
            
    if not zip_match:
        if zip_path in z.namelist():
            zip_match = zip_path
        else:
            return {"size": 0, "blocks": [], "ids": {}, "classes": {}, "error": "Missing zip file"}
            
    try:
        html_data = z.read(zip_match).decode("utf-8", errors="ignore")
        # Remove comments to avoid parser confusion
        html_data = re.sub(r'<!--.*?-->', '', html_data, flags=re.DOTALL)
    except Exception as e:
        return {"size": 0, "blocks": [], "ids": {}, "classes": {}, "error": str(e)}
        
    parser = EPUBHTMLParser(rel_path)
    try:
        parser.feed(html_data)
        parser.close()
    except Exception as e:
        pass
        
    return {
        "size": len(html_data),
        "blocks": parser.blocks,
        "ids": parser.ids,
        "classes": dict(parser.classes),
        "error": None
    }

def unpack_epub_structure(epub_path: str) -> dict:
    """Unpacks EPUB structure, returning metadata, manifest, ordered spine and parsed docs."""
    if not os.path.exists(epub_path):
        raise FileNotFoundError(f"EPUB file not found at: {epub_path}")
        
    with zipfile.ZipFile(epub_path, 'r') as z:
        if "META-INF/container.xml" not in z.namelist():
            raise ValueError("Corrupted EPUB: Missing container.xml")
            
        container_data = z.read("META-INF/container.xml")
        root = ET.fromstring(container_data)
        rootfile = find_elem(root, "rootfile")
        if rootfile is None or "full-path" not in rootfile.attrib:
            raise ValueError("Corrupted EPUB: Missing full-path in container.xml")
            
        opf_path = rootfile.attrib["full-path"]
        opf_dir = os.path.dirname(opf_path)
        
        if opf_path not in z.namelist():
            raise ValueError(f"Corrupted EPUB: Missing OPF file at {opf_path}")
            
        opf_data = z.read(opf_path)
        opf_root = ET.fromstring(opf_data)
        
        # Extract metadata
        metadata = {}
        meta_elem = find_elem(opf_root, "metadata")
        if meta_elem is not None:
            for elem in meta_elem:
                tag = elem.tag.split("}")[-1]
                if tag in ["title", "creator", "publisher", "language"]:
                    metadata[tag] = elem.text or ""
                    
        # Extract manifest
        manifest = {}
        manifest_elem = find_elem(opf_root, "manifest")
        if manifest_elem is not None:
            for item in find_all_elems(manifest_elem, "item"):
                item_id = item.attrib.get("id")
                href = item.attrib.get("href")
                media_type = item.attrib.get("media-type")
                if item_id and href:
                    manifest[item_id] = {
                        "href": urllib.parse.unquote(href),
                        "media_type": media_type
                    }
                    
        # Extract spine
        spine_items = []
        spine_elem = find_elem(opf_root, "spine")
        if spine_elem is not None:
            for itemref in find_all_elems(spine_elem, "itemref"):
                idref = itemref.attrib.get("idref")
                linear = itemref.attrib.get("linear", "yes")
                if idref in manifest:
                    spine_items.append({
                        "id": idref,
                        "href": manifest[idref]["href"],
                        "media_type": manifest[idref]["media_type"],
                        "linear": linear
                    })
                    
        # Parse document structures
        parsed_docs = {}
        for item in spine_items:
            href = item["href"]
            if not item["media_type"] or "html" not in item["media_type"].lower():
                continue
            doc_struct = parse_single_document(z, opf_dir, href)
            parsed_docs[href] = doc_struct
            
        return {
            "metadata": metadata,
            "spine": spine_items,
            "parsed_documents": parsed_docs
        }
