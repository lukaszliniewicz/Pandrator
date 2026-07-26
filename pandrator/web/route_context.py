"""Dependencies shared by domain HTTP route registration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .application_services import ApplicationServices
from .http_lifecycle import ApiGuards


@dataclass(frozen=True, slots=True)
class RouteContext:
    services: ApplicationServices
    guards: ApiGuards
    static_dir: Path
