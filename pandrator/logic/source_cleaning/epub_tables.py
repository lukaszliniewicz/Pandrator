"""Conservative DOM-to-paragraph rendering for EPUB tables.

Only publisher-provided headers establish data relationships. The bounded sparse
grid tracks positions, never expands large spans, and falls back to source rows
when the markup cannot support reliable associations.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass

from bs4 import BeautifulSoup, Tag
from bs4.element import NavigableString

_MAX_WIDTH = 256
_MAX_SPAN = 256
_MAX_GRID_SLOTS = 100_000
_BLOCKS = {
    "p", "div", "li", "ul", "ol", "table", "tr", "td", "th", "section", "article",
    "h1", "h2", "h3", "h4", "h5", "h6", "aside", "blockquote", "dd", "dt",
}


def _text(tag: Tag) -> str:
    clone = copy.deepcopy(tag)
    for annotation in clone.find_all(["rt", "rp", "rtc"]):
        annotation.decompose()
    for line_break in clone.find_all("br"):
        line_break.replace_with(NavigableString(" "))
    for block in list(clone.find_all(list(_BLOCKS))):
        block.insert_before(NavigableString(" "))
        block.insert_after(NavigableString(" "))
        if block.get("id"):
            block.name = "span"
        else:
            block.unwrap()
    return " ".join(clone.get_text().split())


def _append_inline(target: Tag, source: Tag) -> None:
    """Copy cell content, retaining note links, IDs and ruby base markup."""
    clone = copy.deepcopy(source)
    for line_break in clone.find_all("br"):
        line_break.replace_with(NavigableString(" "))
    if clone.get("id"):
        marker = Tag(name="span", attrs={"id": str(clone["id"])})
        target.append(marker)
    for block in list(clone.find_all(list(_BLOCKS))):
        if block.parent is None:
            continue
        block.insert_before(NavigableString(" "))
        block.insert_after(NavigableString(" "))
        if block.get("id"):
            block.name = "span"
        else:
            block.unwrap()
    for node in list(clone.contents):
        target.append(node.extract())
    # Remove only terminal layout whitespace before the cell separator.
    while target.contents and isinstance(target.contents[-1], NavigableString):
        last = target.contents[-1]
        trimmed = str(last).rstrip()
        if trimmed:
            last.replace_with(NavigableString(trimmed))
            break
        last.extract()


def _paragraph(soup: BeautifulSoup, kind: str) -> Tag:
    paragraph = soup.new_tag("p")
    paragraph["data-pandrator-kind"] = kind
    return paragraph


@dataclass
class _Cell:
    tag: Tag
    row: int
    column: int
    width: int
    height: int
    group: int


def _positions(rows: list[Tag], cells: list[list[Tag]]) -> list[list[_Cell]] | None:
    occupied: dict[tuple[int, int], _Cell] = {}
    positioned: list[list[_Cell]] = []
    for row_number, (row, row_cells) in enumerate(zip(rows, cells, strict=True)):
        column = 0
        output_row = []
        for cell in row_cells:
            while (row_number, column) in occupied:
                column += 1
            try:
                width = int(str(cell.get("colspan", "1")))
                height = int(str(cell.get("rowspan", "1")))
            except ValueError:
                return None
            if not (1 <= width <= _MAX_SPAN and 1 <= height <= _MAX_SPAN):
                return None
            if column + width > _MAX_WIDTH:
                return None
            if len(occupied) + width * height > _MAX_GRID_SLOTS:
                return None
            origin = _Cell(cell, row_number, column, width, height, id(row.parent))
            for y in range(row_number, row_number + height):
                for x in range(column, column + width):
                    if (y, x) in occupied:
                        return None
                    occupied[y, x] = origin
            output_row.append(origin)
            column += width
        positioned.append(output_row)
    return positioned


def _header_labels(cell: _Cell, headers: list[_Cell], ids: dict[str, _Cell]) -> list[Tag]:
    explicit = cell.tag.get("headers")
    if explicit:
        tokens = explicit if isinstance(explicit, list) else str(explicit).split()
        return [ids[token].tag for token in tokens if token in ids and ids[token] is not cell]

    labels = []
    for header in headers:
        if header is cell:
            continue
        scope = str(header.tag.get("scope", "")).lower()
        overlap = (
            header.column < cell.column + cell.width
            and cell.column < header.column + header.width
        )
        if scope == "row":
            matches = header.row <= cell.row < header.row + header.height
        elif scope == "rowgroup":
            matches = header.group == cell.group
        elif scope == "colgroup":
            table = header.tag.find_parent("table")
            group_overlap = overlap
            start = 0
            for group in table.find_all("colgroup", recursive=False) if table is not None else []:
                try:
                    columns = group.find_all("col", recursive=False)
                    width = (
                        sum(int(str(column.get("span", "1"))) for column in columns)
                        if columns else int(str(group.get("span", "1")))
                    )
                except ValueError:
                    break
                if not 1 <= width <= _MAX_WIDTH or start + width > _MAX_WIDTH:
                    break
                if start <= header.column < start + width:
                    group_overlap = start <= cell.column < start + width
                    break
                start += width
            matches = group_overlap and header.row <= cell.row
        elif scope == "col":
            matches = overlap and header.row <= cell.row
        else:
            # Only a thead/all-th source row establishes an implicit column
            # header. A lone th next to td is an author-provided row header.
            siblings = header.tag.find_parent("tr")
            header_row = siblings is not None and all(
                item.name == "th" for item in siblings.find_all(["td", "th"], recursive=False)
            )
            in_head = header.tag.find_parent("thead") is not None
            matches = (
                overlap and header.row < cell.row and (header_row or in_head)
            ) or (
                header.row <= cell.row < header.row + header.height
                and header.column < cell.column
                and not header_row and not in_head
            )
        if matches and _text(header.tag):
            labels.append(header.tag)
    return labels


def replace_tables(soup: BeautifulSoup, *, mode: str, labels: dict[str, str]) -> None:
    """Replace outer tables in place; source indexes use the untouched HTML."""
    tables = [table for table in soup.find_all("table") if table.find_parent("table") is None]
    for table in tables:
        presentation = str(table.get("role", "")).lower() in {"presentation", "none"}
        if mode == "skip" and not presentation:
            table.decompose()
            continue
        nested = table.find("table") is not None
        has_headers = (
            any(_text(header) for header in table.find_all("th"))
            or any(_text(head) for head in table.find_all("thead"))
            or any(_text(header) for header in table.find_all(scope=True))
            or str(table.get("role", "")).lower() in {"table", "grid"}
        )
        # Legacy whole-page layout tables carry narrative block boundaries.
        # The existing parser traverses their cells and nested blocks once;
        # replacing them with row paragraphs would erase chapters and pauses.
        if presentation or nested or (
            not has_headers and any(
                cell.find(["h1", "h2", "h3", "h4", "h5", "h6"]) is not None
                or len(cell.find_all("p")) > 1
                for cell in table.find_all(["td", "th"])
            )
        ):
            continue
        rows = [row for row in table.find_all("tr") if row.find_parent("table") is table]
        cells = [
            [cell for cell in row.find_all(["td", "th"]) if cell.find_parent("tr") is row]
            for row in rows
        ]
        # Malformed tables without rows retain all their content as narrative.
        if not rows:
            table.unwrap()
            continue
        positioned = _positions(rows, cells)
        structured = (
            mode == "headers" and positioned is not None and has_headers
        )
        rendered = []
        caption = table.find("caption", recursive=False)
        if structured:
            opening = _paragraph(soup, "table")
            opening.append(labels["table"] + labels["period"])
            if caption is not None:
                opening.append(" ")
                _append_inline(opening, caption)
            rendered.append(opening)
        elif caption is not None:
            paragraph = _paragraph(soup, "table")
            _append_inline(paragraph, caption)
            rendered.append(paragraph)

        headers = [
            cell for row in positioned or [] for cell in row
            if cell.tag.name == "th" or cell.tag.find_parent("thead") is not None
        ]
        ids = {str(cell.tag["id"]): cell for cell in headers if cell.tag.get("id")}
        body_number = 0
        for row_index, row_cells in enumerate(cells):
            if not row_cells:
                continue
            is_heading = structured and (
                all(cell.name == "th" for cell in row_cells)
                or rows[row_index].find_parent("thead") is not None
            )
            cell_texts = [_text(cell) for cell in row_cells]
            paragraph = _paragraph(soup, "table_row")
            if rows[row_index].get("id"):
                paragraph["id"] = str(rows[row_index]["id"])
            if structured and not is_heading and any(cell_texts):
                body_number += 1
                paragraph.append(f'{labels["row"]} {body_number}{labels["period"]} ')
            narrated_cells = 0
            for cell, cell_text in zip(row_cells, cell_texts, strict=True):
                if not cell_text:
                    # Empty cells can carry link targets, but do not create
                    # spoken separators or a punctuation-only narration row.
                    marker = _paragraph(soup, "table_row")
                    _append_inline(marker, cell)
                    for node in list(marker.contents):
                        paragraph.append(node.extract())
                    continue
                if narrated_cells:
                    paragraph.append(labels["separator"])
                if structured and not is_heading and positioned is not None:
                    origin = next(item for item in positioned[row_index] if item.tag is cell)
                    cell_headers = _header_labels(origin, headers, ids)
                    for header_index, header in enumerate(cell_headers):
                        if header_index:
                            paragraph.append(", ")
                        # Header references repeat text, never source IDs/links.
                        paragraph.append(_text(header))
                    if cell_headers:
                        paragraph.append(": ")
                _append_inline(paragraph, cell)
                narrated_cells += 1
            rendered.append(paragraph)
        if structured:
            ending = _paragraph(soup, "table")
            ending.append(labels["end"] + labels["period"])
            rendered.append(ending)
        if table.get("id") and rendered:
            rendered[0]["id"] = str(table["id"])
        for paragraph in rendered:
            table.insert_before(paragraph)
        table.decompose()
