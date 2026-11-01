"""HTTP enrollment routes for native automation clients."""

from __future__ import annotations

import os
import secrets
from datetime import UTC
from html import escape
from pathlib import Path
from urllib.parse import urlencode

from flask import Response, g, jsonify, redirect, request, session

from .access.contracts import AccessContext
from .accounts.browser_auth import establish_browser_session
from .accounts.credential_resources import CredentialResources
from .accounts.execution_actor import actor_from_principal
from .accounts.workspace_queries import WorkspaceQueries
from .auth import Principal
from .automation_enrollment import TTY_REDIRECT_URI, EnrollmentError
from .domain_blueprints import DomainBlueprints
from .models import utcnow
from .projects.sharing_queries import ProjectSharingQueries
from .route_context import RouteContext
from .schemas import AutomationClientCreateRequest


def _is_loopback(value: object) -> bool:
    return str(value or "") in {"127.0.0.1", "::1"}


def register_automation_routes(
    app: DomainBlueprints,
    context: RouteContext,
) -> None:
    """Register consent, token exchange, and revocation endpoints."""

    services = context.services
    enrollment = services.automation_enrollment
    auth = services.auth
    throttle = services.login_throttle
    error_response = context.guards.error_response
    require_auth = context.guards.require_auth

    def html_page(
        title: str,
        body: str,
        *,
        status: int = 200,
    ) -> Response:
        response = Response(
            (
                "<!doctype html><html><head>"
                '<meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width">'
                f"<title>{escape(title)}</title>"
                '</head><body style="font-family:system-ui,sans-serif;'
                "max-width:48rem;margin:3rem auto;padding:0 1rem;"
                'line-height:1.5">'
                f"<h1>{escape(title)}</h1>{body}</body></html>"
            ),
            status=status,
            content_type="text/html; charset=utf-8",
        )
        response.headers["Cache-Control"] = "private, no-store"
        return response

    def authorization_response_uri(
        redirect_uri: str,
        *,
        state: str,
        code: str | None = None,
        error: str | None = None,
    ) -> str:
        values = {"state": state}
        if code:
            values["code"] = code
        if error:
            values["error"] = error
        separator = "&" if "?" in redirect_uri else "?"
        return f"{redirect_uri}{separator}{urlencode(values)}"

    def authorization_result(
        redirect_uri: str,
        response_uri: str,
    ):
        if redirect_uri != TTY_REDIRECT_URI:
            return redirect(response_uri, code=302)
        return html_page(
            "Pandrator enrollment response",
            (
                "<p>Copy the complete one-use response below into the hidden "
                "CLI prompt. It expires in five minutes.</p>"
                '<textarea readonly rows="6" style="width:100%;'
                'font-family:monospace">'
                f"{escape(response_uri)}</textarea>"
                "<p>You may close this page after the CLI confirms that the "
                "credential was stored.</p>"
            ),
        )

    @app.route(
        "/api/v1/auth/automation/authorize",
        methods=["GET", "POST"],
    )
    def auth_automation_authorize():
        """Let a signed-in account approve its own bounded native client."""
        collaboration = auth.collaboration_enabled()
        just_signed_in = False

        if request.method == "GET":
            state = str(request.args.get("state") or "")
            if (
                str(request.args.get("response_type") or "") != "code"
                or not state
                or len(state) > 200
                or any(ord(character) < 32 for character in state)
            ):
                return html_page(
                    "Invalid enrollment request",
                    "<p>The OAuth response type or state value is invalid.</p>",
                    status=400,
                )
            try:
                client_id = enrollment.validate_client_id(request.args.get("client_id"))
                redirect_uri = enrollment.validate_redirect_uri(
                    request.args.get("redirect_uri")
                )
                scopes = enrollment.validate_scopes(request.args.get("scope") or "")
                challenge = enrollment.validate_code_challenge(
                    request.args.get("code_challenge"),
                    request.args.get("code_challenge_method"),
                )
                expires_in_days = max(
                    1,
                    min(
                        int(request.args.get("expires_in_days") or 30),
                        90,
                    ),
                )
            except (EnrollmentError, ValueError) as error:
                return html_page(
                    "Invalid enrollment request",
                    f"<p>{escape(str(error))}</p>",
                    status=400,
                )
            pending = {
                "nonce": secrets.token_urlsafe(32),
                "client_id": client_id,
                "client_name": str(request.args.get("client_name") or "Pandrator MCP")[
                    :160
                ],
                "redirect_uri": redirect_uri,
                "scopes": sorted(scopes),
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": state,
                "expires_in_days": expires_in_days,
            }
            session["automation_authorization"] = pending
        else:
            pending = session.get("automation_authorization")
            if not isinstance(pending, dict) or not secrets.compare_digest(
                str(request.form.get("authorization_nonce") or ""),
                str(pending.get("nonce") or ""),
            ):
                return html_page(
                    "Enrollment request expired",
                    "<p>Restart <code>pandrator-mcp target login</code>.</p>",
                    status=400,
                )
            if request.form.get("decision") != "approve":
                session.pop("automation_authorization", None)
                response_uri = authorization_response_uri(
                    str(pending["redirect_uri"]),
                    state=str(pending["state"]),
                    error="access_denied",
                )
                return authorization_result(
                    str(pending["redirect_uri"]),
                    response_uri,
                )

        pending = session.get("automation_authorization")
        assert isinstance(pending, dict)
        principal = context.guards.principal()
        browser_approved = bool(
            principal is not None
            and principal.kind == "owner_session"
            and principal.browser_session_id is not None
            and principal.has_scope("app.credentials.write" if collaboration else "app.admin")
        )
        if browser_approved and principal is not None and not collaboration:
            try:
                auth.require_administrator(principal)
            except PermissionError:
                browser_approved = False
        if request.method == "POST" and not browser_approved:
            client_key = request.remote_addr or "unknown"
            remote_access = collaboration or not _is_loopback(client_key)
            retry_after = throttle.retry_after(client_key) if remote_access else 0
            password = str(request.form.get("password") or "")
            login_name = str(request.form.get("login_name") or "") if collaboration else None
            account = auth.authenticate_login(login_name, password) if not retry_after else None
            if account is None:
                if remote_access and not retry_after:
                    throttle.record_failure(client_key)
                session["automation_authorization"] = pending
                return html_page(
                    "Pandrator automation consent",
                    "<p>The sign-in details were incorrect or temporarily "
                    "throttled. Return to the authorization URL and try "
                    "again.</p>",
                    status=401,
                )
            if remote_access:
                throttle.reset(client_key)
            try:
                establish_browser_session(
                    auth, account, subject=f"user:{account.user_id}", principal_kind="owner_session", scopes=auth.browser_scopes(account)
                )
            except (PermissionError, ValueError):
                return html_page("Enrollment request expired", "<p>Sign in again to approve enrollment.</p>", status=401)
            session["automation_authorization"] = pending
            g.principal_resolved = False
            principal = context.guards.principal()
            browser_approved = principal is not None
            just_signed_in = collaboration

        if request.method == "POST" and browser_approved and principal is not None and not just_signed_in:
            session.pop("automation_authorization", None)
            identity = services.identity.snapshot(observed_origin=request.url_root)
            try:
                code = enrollment.issue_code(
                    client_id=pending["client_id"],
                    client_name=str(pending["client_name"]),
                    redirect_uri=pending["redirect_uri"],
                    scopes=pending["scopes"],
                    code_challenge=pending["code_challenge"],
                    code_challenge_method=pending["code_challenge_method"],
                    expires_in_days=int(pending["expires_in_days"]),
                    target_instance_id=identity.instance_id,
                    canonical_origin=identity.canonical_origin,
                    approved_by=principal,
                    resource_grants=CredentialResources(
                        workspace_ids=frozenset(request.form.getlist("workspace_id")),
                        project_ids=frozenset(request.form.getlist("project_id")),
                    ) if collaboration else None,
                )
            except (EnrollmentError, ValueError) as error:
                return html_page(
                    "Enrollment failed",
                    f"<p>{escape(str(error))}</p>",
                    status=getattr(error, "status_code", 422),
                )
            g.audit_resource_kind = "automation_client"
            g.audit_resource_id = str(pending["client_id"])
            response_uri = authorization_response_uri(
                str(pending["redirect_uri"]),
                state=str(pending["state"]),
                code=code,
            )
            return authorization_result(
                str(pending["redirect_uri"]),
                response_uri,
            )

        identity = services.identity.snapshot(observed_origin=request.url_root)
        scope_items = "".join(
            f"<li><code>{escape(scope)}</code></li>" for scope in pending["scopes"]
        )
        administrator_notice = (
            "<p><strong>Administrator access requested.</strong> This client can "
            "manage users and password setup links, install or download models, "
            "and manage local services. Project access is assigned separately.</p>"
            if "app.admin" in pending["scopes"]
            else ""
        )
        workspace_fields = ""
        if collaboration and browser_approved and principal is not None:
            actor = actor_from_principal(principal)
            assert actor is not None
            with services.database.snapshot_session() as workspace_session:
                workspaces = WorkspaceQueries.list_workspaces(workspace_session, AccessContext(actor))
                shared_projects = ProjectSharingQueries.shared_with_me(
                    workspace_session, AccessContext(actor), limit=100,
                )
            choices = "".join(
                '<label style="display:block;margin:.5rem 0"><input type="checkbox" name="workspace_id" '
                f'value="{escape(item["id"])}" {"checked" if item["kind"] == "personal" else ""}> '
                f'{escape("My workspace (private)" if item["kind"] == "personal" else item["name"])}</label>'
                for item in workspaces
            )
            workspace_fields = "<fieldset><legend>Approve access to these workspaces</legend>" + choices + "</fieldset>"
            if shared_projects:
                project_choices = "".join(
                    '<label style="display:block;margin:.5rem 0"><input type="checkbox" name="project_id" '
                    f'value="{escape(item["project_id"] or "")}"> {escape(item["name"] or "Project")} · Can view</label>'
                    for item in shared_projects
                )
                workspace_fields += (
                    "<fieldset><legend>Approve access to individual shared projects</legend>"
                    + project_choices + "</fieldset><p>These projects remain read-only. "
                    "No project access is selected automatically.</p>"
                )
        password_field = (
            ""
            if browser_approved
            else (
                ('<label>Login name<br><input name="login_name" required autocomplete="username"></label><br><br>' if collaboration else "")
                + "<label>Password<br>"
                '<input type="password" name="password" required '
                'autocomplete="current-password"></label><br><br>'
            )
        )
        return html_page(
            "Pandrator automation consent",
            (
                "<p>An external native client is asking to control this exact "
                "Pandrator installation.</p>"
                "<dl>"
                f"<dt>Client</dt><dd>{escape(str(pending['client_name']))}</dd>"
                f"<dt>Target ID</dt><dd><code>{escape(identity.instance_id)}</code></dd>"
                f"<dt>Origin</dt><dd><code>{escape(identity.canonical_origin)}</code></dd>"
                "<dt>Credential expiry</dt>"
                f"<dd>{int(pending['expires_in_days'])} days</dd>"
                f"<dt>Exact callback</dt><dd><code>{escape(str(pending['redirect_uri']))}</code></dd>"
                "</dl><p>Requested scopes:</p>"
                f"<ul>{scope_items}</ul>"
                f"{administrator_notice}"
                '<form method="post">'
                '<input type="hidden" name="authorization_nonce" '
                f'value="{escape(str(pending["nonce"]))}">'
                f"{password_field}{workspace_fields}"
                + ("<p>This client may create workspaces and join invitations for your account.</p>" if "app.workspaces.enroll" in pending["scopes"] else "")
                +                 '<button name="decision" value="approve" type="submit">'
                + ("Review workspace access" if collaboration and not browser_approved else "Approve and enroll")
                + "</button> "
                '<button name="decision" value="deny" type="submit">'
                "Deny</button></form>"
            ),
        )

    @app.post("/api/v1/auth/automation/token")
    def auth_automation_token():
        if str(request.form.get("grant_type") or "") != "authorization_code":
            return error_response(
                "unsupported_grant_type",
                "Only the authorization_code grant is supported.",
                400,
            )
        identity = services.identity.snapshot(observed_origin=request.url_root)
        try:
            issued = enrollment.exchange_code(
                code=request.form.get("code"),
                client_id=request.form.get("client_id"),
                redirect_uri=request.form.get("redirect_uri"),
                code_verifier=request.form.get("code_verifier"),
                target_instance_id=identity.instance_id,
                canonical_origin=identity.canonical_origin,
            )
        except EnrollmentError as error:
            return error_response(
                error.code,
                str(error),
                error.status_code,
            )
        expires_at = issued.record.expires_at
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        g.principal_resolved = True
        g.principal = Principal(
            subject=str(issued.record.subject),
            kind="automation_client",
            scopes=frozenset(issued.record.scopes_json or []),
            token_id=issued.record.id,
            network_zone=context.guards._network_zone(),
            target_instance_id=identity.instance_id,
            client_id=issued.client.id,
            user_id=issued.record.user_id,
            auth_revision=issued.record.user_auth_revision,
        )
        g.audit_resource_kind = "automation_client"
        g.audit_resource_id = issued.client.id
        return jsonify(
            {
                "access_token": issued.raw_token,
                "token_type": "Bearer",
                "expires_in": (
                    max(
                        0,
                        int((expires_at - utcnow()).total_seconds()),
                    )
                    if expires_at is not None
                    else None
                ),
                "scope": " ".join(sorted(issued.record.scopes_json or [])),
                "subject": issued.record.subject,
                "client_id": issued.client.id,
                "target_instance_id": identity.instance_id,
                "canonical_origin": identity.canonical_origin,
            }
        )

    @app.get("/api/v1/auth/automation-clients")
    @require_auth
    def automation_client_list():
        return jsonify({"items": enrollment.list_clients(actor=context.guards.access_context().actor)})

    @app.post("/api/v1/auth/automation-clients")
    @require_auth
    def automation_client_register():
        payload = AutomationClientCreateRequest.model_validate(
            request.get_json(silent=True) or {}
        )
        principal = context.guards.principal()
        assert principal is not None
        identity = services.identity.snapshot(observed_origin=request.url_root)
        try:
            client = enrollment.register(
                client_id=payload.client_id,
                name=payload.name,
                redirect_uris=payload.redirect_uris,
                scopes=payload.scopes,
                target_instance_id=identity.instance_id,
                canonical_origin=identity.canonical_origin,
                created_by=principal.subject,
                user_id=principal.user_id,
                auth_revision=principal.auth_revision,
                approval_actor=context.guards.access_context().actor,
            )
        except EnrollmentError as error:
            return error_response(
                error.code,
                str(error),
                error.status_code,
            )
        return (
            jsonify(
                {
                    "id": client.id,
                    "name": client.name,
                    "subject": client.subject,
                    "redirect_uris": client.redirect_uris_json,
                    "allowed_scopes": client.allowed_scopes_json,
                    "target_instance_id": client.target_instance_id,
                    "canonical_origin": client.canonical_origin,
                }
            ),
            201,
        )

    @app.route("/api/v1/automation/local-paths", methods=["GET", "PUT"])
    @context.guards.require_scope("app.admin")
    def automation_local_paths():
        """Inspect or replace the owner-approved local MCP path boundary."""

        principal = context.guards.principal()
        if principal is None or principal.kind != "owner_session":
            return error_response(
                "scope_denied",
                "Only the interactive owner session may change local MCP filesystem access.",
                403,
            )
        try:
            auth.require_administrator(principal)
        except PermissionError:
            return error_response("scope_denied", "Administrator access is required.", 403)
        workspace = str(os.environ.get("PANDRATOR_WORKSPACE") or "").strip()
        if not workspace:
            return error_response(
                "manager_unavailable",
                "The managed workspace path is unavailable.",
                503,
            )
        try:
            from pandrator_mcp.targets import LocalSourceRoot, TargetStore
        except ImportError:
            return error_response(
                "feature_unavailable",
                "This Pandrator installation does not include MCP automation support.",
                501,
            )
        configuration = (
            Path(workspace).expanduser().resolve(strict=False)
            / "Pandrator"
            / "state"
            / "mcp-targets.json"
        )
        store = TargetStore(configuration)
        profiles = store.load(missing_ok=True)
        profile = next(
            (item for item in profiles if item.name == "managed-local"),
            None,
        )
        if profile is None:
            return error_response(
                "not_found",
                "The managed MCP target has not been initialized yet.",
                404,
            )
        if request.method == "PUT":
            payload = request.get_json(silent=True) or {}
            try:
                source_roots = tuple(
                    LocalSourceRoot.model_validate(item)
                    for item in payload.get("source_roots", [])
                )
                output_value = payload.get("output_root")
                output_root = (
                    str(Path(str(output_value)).expanduser().resolve(strict=False))
                    if output_value
                    else None
                )
                profile = store.configure_local_paths(
                    profile.name,
                    source_roots=source_roots,
                    output_root=output_root,
                )
            except (TypeError, ValueError) as error:
                return error_response("validation_error", str(error), 422)
            g.audit_resource_kind = "mcp_local_paths"
            g.audit_resource_id = profile.name
        return jsonify(
            {
                "source_roots": [
                    item.model_dump(mode="json") for item in profile.local_source_roots
                ],
                "output_root": profile.local_output_root,
            }
        )

    @app.delete("/api/v1/auth/automation-clients/<client_id>")
    @require_auth
    def automation_client_revoke(client_id: str):
        try:
            enrollment.revoke(client_id, actor=context.guards.access_context().actor)
        except KeyError:
            return error_response(
                "not_found",
                "Automation client not found.",
                404,
            )
        except EnrollmentError as error:
            return error_response(
                error.code,
                str(error),
                error.status_code,
            )
        g.audit_resource_kind = "automation_client"
        g.audit_resource_id = client_id
        return "", 204
