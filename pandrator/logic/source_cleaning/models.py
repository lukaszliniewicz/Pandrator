from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class SourceBlock:
    """A stable, inspectable unit of source text."""

    block_id: str
    text: str
    line_start: int
    line_end: int
    source_index: int = 0
    href: str | None = None
    page: int | None = None
    tag: str | None = None
    classes: list[str] = field(default_factory=list)
    element_id: str | None = None
    dom_path: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    role_candidates: list[str] = field(default_factory=list)
    raw_markup: str | None = None

    def role_score(self, role: str) -> float:
        evidence = self.attributes.get("role_evidence", {})
        if not isinstance(evidence, dict):
            return 0.0
        payload = evidence.get(str(role))
        if not isinstance(payload, dict):
            return 0.0
        try:
            return float(payload.get("score") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SourceBlock":
        return cls(
            block_id=str(payload.get("block_id") or ""),
            text=str(payload.get("text") or ""),
            line_start=int(payload.get("line_start") or 0),
            line_end=int(payload.get("line_end") or 0),
            source_index=int(payload.get("source_index") or 0),
            href=payload.get("href"),
            page=payload.get("page"),
            tag=payload.get("tag"),
            classes=list(payload.get("classes") or []),
            element_id=payload.get("element_id"),
            dom_path=payload.get("dom_path"),
            attributes=dict(payload.get("attributes") or {}),
            role_candidates=list(payload.get("role_candidates") or []),
            raw_markup=payload.get("raw_markup"),
        )


@dataclass
class SourceDocument:
    """Structured representation used by source-cleaning tools."""

    source_type: str
    source_path: str
    filename: str
    blocks: list[SourceBlock] = field(default_factory=list)
    metadata_candidates: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    language: str = ""
    nav_titles: list[str] = field(default_factory=list)
    navigation_entries: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["blocks"] = [block.to_dict() for block in self.blocks]
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SourceDocument":
        return cls(
            source_type=str(payload.get("source_type") or ""),
            source_path=str(payload.get("source_path") or ""),
            filename=str(payload.get("filename") or ""),
            blocks=[
                SourceBlock.from_dict(block)
                for block in payload.get("blocks", [])
                if isinstance(block, dict)
            ],
            metadata_candidates=dict(payload.get("metadata_candidates") or {}),
            language=str(payload.get("language") or ""),
            nav_titles=list(payload.get("nav_titles") or []),
            navigation_entries=[
                dict(entry)
                for entry in payload.get("navigation_entries", [])
                if isinstance(entry, dict)
            ],
            warnings=list(payload.get("warnings") or []),
            attributes=dict(payload.get("attributes") or {}),
        )

    def plain_lines(self) -> list[str]:
        return [block.text for block in self.blocks if block.text.strip()]

    def plain_text(self) -> str:
        return "\n\n".join(self.plain_lines())

    def block_by_id(self, block_id: str) -> SourceBlock | None:
        normalized = str(block_id or "")
        return next((block for block in self.blocks if block.block_id == normalized), None)

    def blocks_in_line_range(self, start_line: int, end_line: int) -> list[SourceBlock]:
        start = max(1, int(start_line))
        end = max(start, int(end_line))
        return [
            block
            for block in self.blocks
            if block.line_start <= end and block.line_end >= start
        ]

    def excluding_blocks(self, block_ids: set[str]) -> "SourceDocument":
        return SourceDocument(
            source_type=self.source_type,
            source_path=self.source_path,
            filename=self.filename,
            blocks=[block for block in self.blocks if block.block_id not in block_ids],
            metadata_candidates=self.metadata_candidates,
            language=self.language,
            nav_titles=self.nav_titles,
            navigation_entries=self.navigation_entries,
            warnings=self.warnings,
            attributes=self.attributes,
        )


@dataclass
class SearchHit:
    hit_id: str
    block_id: str
    line_start: int
    line_end: int
    snippet: str
    match_text: str
    href: str | None = None
    page: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CleaningResult:
    cleaned_text: str
    metadata: dict[str, str]
    applied_operations: list[dict[str, Any]]
    skipped_operations: list[dict[str, Any]]
    warnings: list[str]
    deleted_block_ids: list[str]
    diff_text: str
    report: dict[str, Any]


@dataclass
class PhaseResult:
    """Outcome of a single pipeline phase."""

    phase_name: str
    phase_description: str
    operations: list[dict[str, Any]] = field(default_factory=list)
    iterations: int = 0
    max_iterations: int = 0
    warnings: list[str] = field(default_factory=list)
    llm_usage: dict[str, Any] = field(default_factory=dict)
    stopped: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PipelineResult:
    """Aggregated outcome of the full multi-phase cleaning pipeline."""

    phases: list[PhaseResult] = field(default_factory=list)
    all_operations: list[dict[str, Any]] = field(default_factory=list)
    total_iterations: int = 0
    warnings: list[str] = field(default_factory=list)
    llm_usage: dict[str, Any] = field(default_factory=dict)
    stopped: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
