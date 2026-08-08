"""Flask application factory for the browser and API clients."""

from __future__ import annotations

import os
from pathlib import Path

from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

from .api_routes import register_routes
from .application_services import ApplicationServices
from .auth import BootstrapTokenStore
from .http_lifecycle import (
    ApiGuards,
    frontend_script_policy,
    load_or_create_flask_secret,
)
from .route_context import RouteContext


def create_app(
    *,
    data_root: str | os.PathLike[str] | None = None,
    testing: bool = False,
    trusted_hosts: list[str] | None = None,
    proxy_hops: int = 0,
    secure_cookies: bool = False,
    bootstrap_tokens: BootstrapTokenStore | None = None,
    capability_ttl_seconds: int | None = None,
    background_maintenance: bool | None = None,
    public_origin: str | None = None,
) -> Flask:
    """Compose the dependency graph, HTTP lifecycle, and domain routes."""

    services = ApplicationServices.build(
        data_root=data_root,
        bootstrap_tokens=bootstrap_tokens,
        capability_ttl_seconds=capability_ttl_seconds,
        public_origin=public_origin,
    )
    # Repair interrupted job/domain transitions once at startup. Normal API
    # reads stay lock-free; terminal transitions update only their owner.
    services.jobs.reconcile()
    static_dir = Path(__file__).with_name("static")
    app = Flask(
        __name__,
        static_folder=str(static_dir),
        static_url_path="/assets",
    )
    app.config.update(
        SECRET_KEY=load_or_create_flask_secret(services.paths),
        TESTING=testing,
        MAX_CONTENT_LENGTH=10 * 1024 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=secure_cookies,
        TRUSTED_HOSTS=trusted_hosts or ["localhost", "127.0.0.1", "[::1]"],
    )
    if proxy_hops:
        app.wsgi_app = ProxyFix(
            app.wsgi_app,
            x_for=proxy_hops,
            x_proto=proxy_hops,
            x_host=proxy_hops,
            x_port=proxy_hops,
        )

    app.extensions["pandrator"] = services.extension_mapping()
    guards = ApiGuards(
        app,
        services,
        testing=testing,
        script_policy=frontend_script_policy(static_dir),
    )
    guards.register()
    register_routes(
        app,
        RouteContext(
            services=services,
            guards=guards,
            static_dir=static_dir,
        ),
    )

    maintenance_enabled = (
        not testing if background_maintenance is None else background_maintenance
    )
    if maintenance_enabled:
        services.startup_maintenance.start()
    return app
