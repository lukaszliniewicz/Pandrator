"""Explicit resource grants for non-interactive credential owners."""

from __future__ import annotations

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from ..model_base import Base


class ApiTokenWorkspaceGrant(Base):
    __tablename__ = "api_token_workspace_grants"
    __table_args__ = (Index("ix_api_token_workspace_grants_workspace_id", "workspace_id"),)

    token_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "api_tokens.id",
            name="fk_api_token_workspace_grants_token_id",
            ondelete="CASCADE",
        ),
        primary_key=True,
        nullable=False,
    )
    workspace_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "workspaces.id",
            name="fk_api_token_workspace_grants_workspace_id",
            ondelete="CASCADE",
        ),
        primary_key=True,
        nullable=False,
    )


class ApiTokenProjectGrant(Base):
    __tablename__ = "api_token_project_grants"
    __table_args__ = (Index("ix_api_token_project_grants_project_id", "project_id"),)

    token_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "api_tokens.id",
            name="fk_api_token_project_grants_token_id",
            ondelete="CASCADE",
        ),
        primary_key=True,
        nullable=False,
    )
    project_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "projects.id",
            name="fk_api_token_project_grants_project_id",
            ondelete="CASCADE",
        ),
        primary_key=True,
        nullable=False,
    )


class AutomationClientWorkspaceGrant(Base):
    __tablename__ = "automation_client_workspace_grants"
    __table_args__ = (Index("ix_automation_client_workspace_grants_workspace_id", "workspace_id"),)

    client_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "automation_clients.id",
            name="fk_automation_client_workspace_grants_client_id",
            ondelete="CASCADE",
        ),
        primary_key=True,
        nullable=False,
    )
    workspace_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "workspaces.id",
            name="fk_automation_client_workspace_grants_workspace_id",
            ondelete="CASCADE",
        ),
        primary_key=True,
        nullable=False,
    )


class AutomationClientProjectGrant(Base):
    __tablename__ = "automation_client_project_grants"
    __table_args__ = (Index("ix_automation_client_project_grants_project_id", "project_id"),)

    client_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "automation_clients.id",
            name="fk_automation_client_project_grants_client_id",
            ondelete="CASCADE",
        ),
        primary_key=True,
        nullable=False,
    )
    project_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "projects.id",
            name="fk_automation_client_project_grants_project_id",
            ondelete="CASCADE",
        ),
        primary_key=True,
        nullable=False,
    )


class BrowserAuthWorkspaceGrant(Base):
    __tablename__ = "browser_auth_workspace_grants"
    __table_args__ = (Index("ix_browser_auth_workspace_grants_workspace_id", "workspace_id"),)

    browser_session_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "browser_auth_sessions.id",
            name="fk_browser_auth_workspace_grants_browser_session_id",
            ondelete="CASCADE",
        ),
        primary_key=True,
        nullable=False,
    )
    workspace_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "workspaces.id",
            name="fk_browser_auth_workspace_grants_workspace_id",
            ondelete="CASCADE",
        ),
        primary_key=True,
        nullable=False,
    )


class BrowserAuthProjectGrant(Base):
    __tablename__ = "browser_auth_project_grants"
    __table_args__ = (Index("ix_browser_auth_project_grants_project_id", "project_id"),)

    browser_session_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "browser_auth_sessions.id",
            name="fk_browser_auth_project_grants_browser_session_id",
            ondelete="CASCADE",
        ),
        primary_key=True,
        nullable=False,
    )
    project_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "projects.id",
            name="fk_browser_auth_project_grants_project_id",
            ondelete="CASCADE",
        ),
        primary_key=True,
        nullable=False,
    )


class AutomationCodeWorkspaceGrant(Base):
    __tablename__ = "automation_code_workspace_grants"
    __table_args__ = (Index("ix_automation_code_workspace_grants_workspace_id", "workspace_id"),)

    code_grant_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "automation_enrollment_grants.id",
            name="fk_automation_code_workspace_grants_code_grant_id",
            ondelete="CASCADE",
        ),
        primary_key=True,
        nullable=False,
    )
    workspace_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "workspaces.id",
            name="fk_automation_code_workspace_grants_workspace_id",
            ondelete="CASCADE",
        ),
        primary_key=True,
        nullable=False,
    )


class AutomationCodeProjectGrant(Base):
    __tablename__ = "automation_code_project_grants"
    __table_args__ = (Index("ix_automation_code_project_grants_project_id", "project_id"),)

    code_grant_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "automation_enrollment_grants.id",
            name="fk_automation_code_project_grants_code_grant_id",
            ondelete="CASCADE",
        ),
        primary_key=True,
        nullable=False,
    )
    project_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "projects.id",
            name="fk_automation_code_project_grants_project_id",
            ondelete="CASCADE",
        ),
        primary_key=True,
        nullable=False,
    )
