from __future__ import annotations

import os
import posixpath
import re
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser


@dataclass(frozen=True)
class EpubSourceProbe:
    narrative_document_count: int = 0
    encrypted_narrative_resources: tuple[str, ...] = ()
    raw_visible_text_chars: int = 0
    image_count: int = 0


@dataclass(frozen=True)
class EpubExtractionResult:
    text: str
    category: str
    error_code: str | None
    message: str
    probe: EpubSourceProbe

    @property
    def ok(self) -> bool:
        return self.category == "normal"

    def require_text(self) -> str:
        if not self.ok:
            raise EpubExtractionError(self)
        return self.text

    @classmethod
    def success(cls, text: str) -> EpubExtractionResult:
        return cls(
            text=text,
            category="normal",
            error_code=None,
            message="EPUB narrative text was extracted.",
            probe=EpubSourceProbe(),
        )


class EpubExtractionError(RuntimeError):
    def __init__(self, result: EpubExtractionResult):
        self.result = result
        self.code = result.error_code or "epub_extraction_failed"
        super().__init__(result.message)


class _VisibleTextProbe(HTMLParser):
    _SUPPRESSED_TAGS = frozenset({"head", "script", "style", "svg"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._suppressed_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, _attrs) -> None:
        if tag.lower() in self._SUPPRESSED_TAGS:
            self._suppressed_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self._SUPPRESSED_TAGS and self._suppressed_depth:
            self._suppressed_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._suppressed_depth and data.strip():
            self.parts.append(data)


def inspect_epub_source(
    epub_path: str,
    *,
    analyze_content: bool = False,
) -> EpubSourceProbe:
    """Inspect package evidence without treating publisher conformance as recovery.

    The ordinary successful path only needs the lightweight package/encryption
    scan. Raw visible-text and image counts are calculated only after an empty
    extraction, avoiding a second full HTML parse for normal books.
    """
    if not os.path.exists(epub_path):
        raise FileNotFoundError(f"EPUB file not found at: {epub_path}")

    with zipfile.ZipFile(epub_path, "r") as archive:
        names = {name.lower(): name for name in archive.namelist()}
        container_name = names.get("meta-inf/container.xml")
        if not container_name:
            raise ValueError("Corrupted EPUB: Missing container.xml")

        container_root = ET.fromstring(archive.read(container_name))
        rootfile = _first_local(container_root, "rootfile")
        if rootfile is None or not rootfile.attrib.get("full-path"):
            raise ValueError("Corrupted EPUB: Missing full-path in container.xml")

        opf_path = _normalized_archive_path(rootfile.attrib["full-path"])
        opf_name = names.get(opf_path.lower())
        if not opf_name:
            raise ValueError(f"Corrupted EPUB: Missing OPF file at {opf_path}")

        opf_root = ET.fromstring(archive.read(opf_name))
        opf_dir = posixpath.dirname(opf_path)
        manifest: dict[str, tuple[str, str]] = {}
        for item in _all_local(opf_root, "item"):
            item_id = str(item.attrib.get("id") or "")
            href = str(item.attrib.get("href") or "")
            media_type = str(item.attrib.get("media-type") or "")
            if item_id and href:
                archive_path = _normalized_archive_path(
                    posixpath.join(opf_dir, urllib.parse.unquote(href))
                )
                manifest[item_id] = (archive_path, media_type)

        narrative_paths: list[str] = []
        for itemref in _all_local(opf_root, "itemref"):
            idref = str(itemref.attrib.get("idref") or "")
            manifest_item = manifest.get(idref)
            if manifest_item and "html" in manifest_item[1].lower():
                narrative_paths.append(manifest_item[0])

        encrypted_paths = _encrypted_paths(archive, names)
        encrypted_narrative = tuple(
            path
            for path in narrative_paths
            if _matches_encrypted_resource(path, encrypted_paths)
        )

        raw_visible_text_chars = 0
        image_count = 0
        if analyze_content and not encrypted_narrative:
            for archive_path in narrative_paths:
                resource_name = names.get(archive_path.lower())
                if not resource_name:
                    continue
                raw = archive.read(resource_name)
                image_count += len(
                    re.findall(rb"<(?:img|image)\b", raw, flags=re.IGNORECASE)
                )
                decoded = raw.decode("utf-8", errors="ignore")
                probe = _VisibleTextProbe()
                try:
                    probe.feed(decoded)
                    probe.close()
                    visible = " ".join(probe.parts)
                except (AssertionError, UnicodeError, ValueError):
                    visible = re.sub(r"<[^>]+>", " ", decoded)
                raw_visible_text_chars += len(re.sub(r"\s+", " ", visible).strip())

    return EpubSourceProbe(
        narrative_document_count=len(narrative_paths),
        encrypted_narrative_resources=encrypted_narrative,
        raw_visible_text_chars=raw_visible_text_chars,
        image_count=image_count,
    )


def classify_epub_extraction(
    text: str,
    probe: EpubSourceProbe,
) -> EpubExtractionResult:
    if probe.encrypted_narrative_resources:
        return EpubExtractionResult(
            text="",
            category="encrypted",
            error_code="epub_encrypted",
            message=(
                "This EPUB encrypts or obfuscates narrative resources. Pandrator "
                "cannot extract them without the required keys."
            ),
            probe=probe,
        )

    if text.strip():
        return EpubExtractionResult(
            text=text,
            category="normal",
            error_code=None,
            message="EPUB narrative text was extracted.",
            probe=probe,
        )

    image_text_ceiling = max(500, probe.image_count * 100)
    if probe.image_count and probe.raw_visible_text_chars <= image_text_ceiling:
        return EpubExtractionResult(
            text="",
            category="image_only",
            error_code="epub_ocr_required",
            message=(
                "This EPUB appears to contain page images rather than extractable "
                "narrative text. OCR is required before source cleaning."
            ),
            probe=probe,
        )

    if probe.raw_visible_text_chars >= 200:
        return EpubExtractionResult(
            text="",
            category="parser_empty",
            error_code="epub_parser_empty",
            message=(
                "The EPUB contains readable source text, but Pandrator produced an "
                "empty extraction. No source-cleaning run was created."
            ),
            probe=probe,
        )

    return EpubExtractionResult(
        text="",
        category="empty",
        error_code="epub_empty",
        message="The EPUB contains no extractable narrative text.",
        probe=probe,
    )


def _first_local(root: ET.Element, name: str) -> ET.Element | None:
    for element in root.iter():
        if element.tag.split("}")[-1] == name:
            return element
    return None


def _all_local(root: ET.Element, name: str) -> list[ET.Element]:
    return [element for element in root.iter() if element.tag.split("}")[-1] == name]


def _normalized_archive_path(path: str) -> str:
    return posixpath.normpath(path.replace("\\", "/")).lstrip("/")


def _encrypted_paths(
    archive: zipfile.ZipFile,
    names: dict[str, str],
) -> tuple[str, ...]:
    encryption_name = names.get("meta-inf/encryption.xml")
    if not encryption_name:
        return ()
    root = ET.fromstring(archive.read(encryption_name))
    paths: list[str] = []
    for reference in _all_local(root, "CipherReference"):
        uri = str(reference.attrib.get("URI") or "")
        if not uri:
            continue
        decoded = urllib.parse.unquote(urllib.parse.urlsplit(uri).path)
        paths.append(_normalized_archive_path(decoded))
    return tuple(dict.fromkeys(path for path in paths if path and path != "."))


def _matches_encrypted_resource(
    archive_path: str,
    encrypted_paths: tuple[str, ...],
) -> bool:
    lowered = archive_path.lower()
    return any(
        lowered == candidate.lower()
        or lowered.endswith(f"/{candidate.lower()}")
        or candidate.lower().endswith(f"/{lowered}")
        for candidate in encrypted_paths
    )
