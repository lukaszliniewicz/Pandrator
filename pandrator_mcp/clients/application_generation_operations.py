"""Application requests for generation-operation lifecycle reads."""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import quote

from .application_requests import ApplicationRequests


class ApplicationGenerationOperationMethods(ApplicationRequests):
    def get_generation_operation(
        self,
        operation_id: str,
        include_coverage: bool = True,
    ) -> dict[str, Any]:
        """Read one stable operation ID through the fixed lifecycle endpoint."""

        return self._request_json(
            f"/api/v1/generation-operations/{quote(str(operation_id), safe='')}",
            parameters={"include_coverage": "true" if include_coverage else "false"},
            read_only_operation=True,
        )

    def get_generation_run_coverage(
        self,
        generation_run_id: str,
        audio_version_side: Literal["before", "after"] = "after",
    ) -> dict[str, Any]:
        """Read coverage for a selected run and optional pinned audio side."""

        if audio_version_side not in {"before", "after"}:
            raise ValueError("Audio version side must be before or after.")
        return self._request_json(
            f"/api/v1/generation-runs/{quote(str(generation_run_id), safe='')}/coverage",
            parameters={"audio_version_side": audio_version_side},
            read_only_operation=True,
        )


__all__ = ["ApplicationGenerationOperationMethods"]
