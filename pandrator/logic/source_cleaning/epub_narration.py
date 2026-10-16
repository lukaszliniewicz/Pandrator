"""Author-supplied EPUB table and image semantics for spoken extraction only."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, Tag
from bs4.element import NavigableString

from .epub_tables import replace_tables

EPUB_TABLE_MODES = ("headers", "rows", "skip")
EPUB_IMAGE_MODES = ("descriptions", "captions", "skip")

_LABELS = {
    "en": ("Table", "End table", "Row", "Image"),
    "pl": ("Tabela", "Koniec tabeli", "Wiersz", "Ilustracja"),
    "fr": ("Tableau", "Fin du tableau", "Ligne", "Illustration"),
    "nl": ("Tabel", "Einde van de tabel", "Rij", "Afbeelding"),
    "ja": ("表", "表の終わり", "行", "図"),
    "de": ("Tabelle", "Ende der Tabelle", "Zeile", "Abbildung"),
}
_LANGUAGES = {
    "eng": "en", "pol": "pl", "fra": "fr", "fre": "fr", "nld": "nl", "dut": "nl",
    "jpn": "ja", "deu": "de", "ger": "de",
}
_RELEVANT = re.compile(
    r"<(?:table|img|figure|figcaption)\b|"
    r"\bclass\s*=\s*(?:\"[^\"]*caption|'[^']*caption|[^\s>\"']*caption)",
    re.I,
)
_CAPTION_CLASS = re.compile(r"^(?:figcaption|caption\d*|image-caption|figure-caption)$", re.I)
_GENERIC = re.compile(
    r"^(?:image|cover|illustration|figure|fig|photo|photograph|picture|img|text|"
    r"ilustracja|ilustracje|obraz|obrazek|zdjęcie|okładka|rysunek|"
    r"abbildung|bild|foto|fotografie|umschlag|"
    r"afbeelding|omslag|illustratie|"
    r"couverture|photographie|図|画像|表紙|挿絵)"
    r"(?:\s*[.\-:#]?\s*(?:\d+|[ivxlcdm]+))?$",
    re.I,
)
_FILE_LABEL = re.compile(
    r"(?:https?://\S+|www\.\S+|\S*[/\\]\S*|"
    r"[^\s/\\]+\.(?:jpe?g|png|gif|svg|webp|bmp|tiff?|avif)(?:[?#]\S*)?)", re.I
)
_AUTO_DESCRIPTION = re.compile(
    r"\s*Description automatically generated(?: with (?:low|medium|high) confidence)?\.?\s*$",
    re.I,
)


def validate_narration_modes(table_mode: str, image_mode: str) -> None:
    if table_mode not in EPUB_TABLE_MODES:
        raise ValueError(f"Unknown EPUB table mode: {table_mode!r}")
    if image_mode not in EPUB_IMAGE_MODES:
        raise ValueError(f"Unknown EPUB image mode: {image_mode!r}")


def _normalized(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.casefold()).split())


def _meaningful(text: str) -> bool:
    normalized = text.strip().strip("[]() .:;!?")
    return bool(normalized) and not _GENERIC.fullmatch(normalized) and not _FILE_LABEL.fullmatch(normalized)


def _visible_text(tag: Tag) -> str:
    # No spaces are inserted at inline boundaries (e.g. ruby base + suffix).
    return " ".join(
        "".join(
            str(node) for node in tag.descendants
            if isinstance(node, NavigableString)
            and node.find_parent(["rt", "rp", "rtc", "script", "style"]) is None
        ).split()
    )


def _is_caption(tag: Tag) -> bool:
    classes = tag.get("class") or []
    if isinstance(classes, str):
        classes = classes.split()
    return tag.name == "figcaption" or any(
        _CAPTION_CLASS.fullmatch(str(value)) for value in classes
    )


def _hidden(tag: Tag) -> bool:
    if tag.name == "img" and str(tag.get("role", "")).strip().lower() in {"presentation", "none"}:
        return True
    return any(
        str(parent.get("aria-hidden", "")).strip().lower() == "true"
        or parent.has_attr("hidden")
        for parent in [tag, *tag.parents] if isinstance(parent, Tag)
    )


def _mark_caption(tag: Tag) -> None:
    # A caption may wrap p/li elements. Mark all parser blocks so a visual
    # ancestor class cannot discard the publisher's actual caption text.
    tag["data-pandrator-kind"] = "image_caption"
    if tag.name not in {"p", "div", "li"}:
        tag.name = "div"
    for child in tag.find_all(["p", "div", "li"]):
        child["data-pandrator-kind"] = "image_caption"


def _replace_images(soup: BeautifulSoup, *, mode: str, label: str) -> None:
    captions = [tag for tag in soup.find_all(True) if _is_caption(tag)]
    for caption in captions:
        if caption.parent is None:
            continue
        if mode != "skip" and not _hidden(caption) and _meaningful(_visible_text(caption)):
            _mark_caption(caption)
        elif mode != "skip":
            caption.decompose()

    for image in list(soup.find_all("img")):
        if image.parent is None:
            continue
        if _hidden(image) or image.get("alt") == "":
            image.decompose()
            continue
        alternative = _AUTO_DESCRIPTION.sub(
            "", str(image.get("aria-label") or image.get("alt") or ""),
        ).strip()
        block = image.find_parent(["p", "li", "dd", "dt", "td", "th"])
        if (block is not None and len(alternative) == 1 and alternative.isalpha()
                and _visible_text(block)):
            # A dropcap supplies a letter of the narrative in every image
            # mode; filtering illustrations must never remove that letter.
            following = image.next_sibling
            if isinstance(following, NavigableString):
                following.replace_with(NavigableString(str(following).lstrip()))
            image.replace_with(NavigableString(alternative))
            continue
        if mode != "descriptions":
            image.decompose()
            continue
        if not alternative and image.get("aria-labelledby"):
            # Referenced prose/captions will be narrated at their own DOM
            # position. Repeating them here would duplicate the source.
            referenced = [soup.find(id=token) for token in str(image["aria-labelledby"]).split()]
            unnarrated = [
                tag for tag in referenced if tag is not None and not _hidden(tag)
                and tag.name not in {"p", "div", "li", "figcaption", "td", "th"}
                and tag.find_parent(["p", "div", "li", "figcaption", "td", "th"]) is None
            ]
            alternative = " ".join(_visible_text(tag) for tag in unnarrated)
        if not _meaningful(alternative):
            image.decompose()
            continue
        # Some books put the exact image alternative in an ordinary adjacent
        # paragraph rather than a figcaption. Narrate that source paragraph once.
        nearby = [image.find_previous_sibling(), image.find_next_sibling()]
        if any(
            tag is not None and tag.name in {"p", "figcaption"}
            and not _hidden(tag) and _normalized(alternative) == _normalized(_visible_text(tag))
            for tag in nearby
        ):
            image.decompose()
            continue
        figure = image.find_parent("figure")
        if figure is not None and any(
            _normalized(alternative) == _normalized(_visible_text(caption))
            for caption in figure.find_all(True) if caption.get("data-pandrator-kind")
        ):
            image.decompose()
            continue
        if block is not None:
            # Preserve inline word boundaries and paragraph identity. If a
            # publisher repeats the same description in the prose, use it once.
            if _normalized(alternative) == _normalized(_visible_text(block)):
                image.decompose()
                continue
            replacement = soup.new_tag("span")
            replacement["data-pandrator-kind"] = "image_description"
            replacement.string = f" {label}: {alternative} "
        else:
            replacement = soup.new_tag("p")
            replacement["data-pandrator-kind"] = "image_description"
            replacement.string = f"{label}: {alternative}"
        if image.get("id"):
            replacement["id"] = str(image["id"])
        image.replace_with(replacement)


def prepare_narration_html(
    html: str, *, table_mode: str, image_mode: str, language: str,
) -> str:
    """Return narration markup without modifying the EPUB or structural index."""
    validate_narration_modes(table_mode, image_mode)
    if not _RELEVANT.search(html):
        return html
    from ..language_capabilities import canonical_language_tag

    try:
        canonical = canonical_language_tag(language)
    except ValueError:
        canonical = ""
    base = canonical.split("-", 1)[0]
    base = _LANGUAGES.get(base, base)
    table, end, row, image = _LABELS.get(base, _LABELS["en"])
    soup = BeautifulSoup(html, "html.parser")
    _replace_images(soup, mode=image_mode, label=image)
    replace_tables(soup, mode=table_mode, labels={
        "table": table, "end": end, "row": row,
        "period": "。" if base == "ja" else ".",
        "separator": "、" if base == "ja" else "; ",
    })
    return str(soup)
