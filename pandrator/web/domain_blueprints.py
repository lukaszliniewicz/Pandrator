"""Domain ownership for Flask routes."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from flask import Blueprint, Flask


DOMAIN_ORDER = (
    "system",
    "auth",
    "tts",
    "sessions",
    "generation",
    "workflow",
    "jobs",
    "media",
    "providers",
    "library",
    "frontend",
)


def route_domain(rule: str) -> str:
    """Map a public URL rule to its owning backend domain."""

    if rule.startswith("/api/v1/auth/"):
        return "auth"
    if rule.startswith("/api/v1/services/tts"):
        return "tts"
    if rule.startswith(("/api/v1/providers", "/api/v1/credential")):
        return "providers"
    if rule.startswith(
        (
            "/api/v1/pronunciations",
            "/api/v1/voices",
            "/api/v1/rvc",
            "/api/v1/training",
        )
    ):
        return "library"
    if rule.startswith(("/api/v1/jobs", "/api/v1/events")):
        return "jobs"
    if rule.startswith(
        (
            "/api/v1/uploads",
            "/api/v1/sources",
            "/api/v1/artifacts",
            "/api/v1/document-revisions",
        )
    ) or "/sources" in rule or "/outputs/" in rule or "/pdf/" in rule:
        return "media"
    if rule.startswith(
        (
            "/api/v1/generation-runs",
            "/api/v1/generation-segments",
        )
    ) or any(
        marker in rule
        for marker in (
            "/generation-plan",
            "/generation-runs",
            "/generation-segments",
            "/output-assemblies",
        )
    ):
        return "generation"
    if rule.startswith(
        (
            "/api/v1/agent-runs",
            "/api/v1/session-bundles",
        )
    ) or any(
        marker in rule
        for marker in (
            "/agent-runs",
            "/bundle",
            "/workflow",
            "/stages/",
            "/subtitles",
        )
    ):
        return "workflow"
    if rule.startswith(
        (
            "/api/v1/sessions",
            "/api/v1/defaults",
            "/api/v1/settings",
        )
    ):
        return "sessions"
    if rule.startswith("/api/v1/"):
        return "system"
    return "frontend"


class DomainBlueprints:
    """Decorator-compatible router that assigns rules to domain Blueprints."""

    def __init__(self, app: Flask):
        self.config = app.config
        self._blueprints = {
            domain: Blueprint(domain, __name__)
            for domain in DOMAIN_ORDER
        }
        self._registered = False

    @property
    def blueprints(self) -> tuple[Blueprint, ...]:
        return tuple(self._blueprints[name] for name in DOMAIN_ORDER)

    def _decorator(
        self,
        method: str,
        rule: str,
        **options: Any,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        blueprint = self._blueprints[route_domain(rule)]
        return getattr(blueprint, method)(rule, **options)

    def get(self, rule: str, **options: Any):
        return self._decorator("get", rule, **options)

    def post(self, rule: str, **options: Any):
        return self._decorator("post", rule, **options)

    def put(self, rule: str, **options: Any):
        return self._decorator("put", rule, **options)

    def patch(self, rule: str, **options: Any):
        return self._decorator("patch", rule, **options)

    def delete(self, rule: str, **options: Any):
        return self._decorator("delete", rule, **options)

    def register(self, app: Flask) -> None:
        if self._registered:
            raise RuntimeError("Domain Blueprints have already been registered.")
        for blueprint in self.blueprints:
            app.register_blueprint(blueprint)
        self._registered = True
