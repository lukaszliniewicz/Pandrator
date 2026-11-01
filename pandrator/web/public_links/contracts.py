"""Transport-independent public-link results and bounded errors."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..accounts.execution_actor import UserActor

LINK_SCOPES = {
    "manage_project": frozenset({"app.read", "app.write", "app.run", "app.cancel"}),
    "review_translation": frozenset({"app.read", "app.write"}),
}
SETTING_KEY = "public.links"


class PublicLinkError(Exception):
    def __init__(self, code: str, status_code: int = 400):
        self.code = code
        self.status_code = status_code
        super().__init__(code.replace("_", " "))


@dataclass(frozen=True, slots=True)
class RedeemedProjectLink:
    raw_browser_session: str = field(repr=False)
    csrf_token: str = field(repr=False)
    link: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ProjectLinkContext:
    actor: UserActor
    link: dict[str, Any]
    csrf_hash: str = field(repr=False)
