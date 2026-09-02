# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import time
import uuid

from flask import Flask, jsonify, redirect, request, session, url_for

PUBLIC_ENDPOINTS = frozenset({"auth.login", "healthz", "static"})
PASSWORD_CHANGE_ENDPOINTS = frozenset({"auth.logout", "users.change_password", "static"})


def create_app(test_config=None):
    base_dir = os.path.dirname(os.path.abspath(__file__))
    app = Flask(
        __name__,
        template_folder=os.path.join(base_dir, "templates"),
        static_folder=os.path.join(base_dir, "static"),
    )
    app.config.from_object("config")
    if test_config:
        app.config.update(test_config)
        if "LOG_FILE" not in test_config:
            app.config["LOG_FILE"] = os.path.join(app.config["DATA_DIR"], "logs", "app.jsonl")
        if "FILE_STORAGE_ROOT" not in test_config:
            app.config["FILE_STORAGE_ROOT"] = os.path.join(app.config["DATA_DIR"], "files")

    from app.ai.settings import validate_assistant_settings

    validate_assistant_settings(app.config)

    if not app.config.get("TESTING"):
        secret_key = os.environ.get("FLASK_SECRET_KEY")
        if not secret_key:
            raise RuntimeError("FLASK_SECRET_KEY must be configured for non-testing environments")
        app.config["SECRET_KEY"] = secret_key

    engine = app.config.get("DATABASE_ENGINE")
    database_url = app.config.get("DATABASE_URL") or os.environ.get("DATABASE_URL")
    if engine is None and database_url and not app.config.get("TESTING"):
        from app.db import initialize_runtime_database

        engine = initialize_runtime_database(database_url)
    if engine is not None:
        app.extensions["database_engine"] = engine

    app.config["SESSION_TYPE"] = "cachelib"
    app.config.setdefault("SESSION_FILE_DIR", os.path.join(app.config["DATA_DIR"], "flask_sessions"))
    app.config["SESSION_PERMANENT"] = True
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config.setdefault("SESSION_COOKIE_SAMESITE", "Lax")
    app.config.setdefault("SESSION_COOKIE_SECURE", False)
    app.config.setdefault("SESSION_REFRESH_EACH_REQUEST", True)
    os.makedirs(app.config["SESSION_FILE_DIR"], exist_ok=True)
    from cachelib.file import FileSystemCache

    app.config["SESSION_CACHELIB"] = FileSystemCache(
        cache_dir=app.config["SESSION_FILE_DIR"],
        threshold=int(app.config.get("SESSION_FILE_THRESHOLD", 500)),
    )
    from flask_session import Session

    Session(app)

    users_repository = app.config.get("USERS_REPOSITORY")
    audit_service = app.config.get("AUDIT_SERVICE")
    security_enabled = app.config.get("SECURITY_AUTH_ENABLED")
    if security_enabled is None:
        security_enabled = users_repository is not None or engine is not None
    app.config["SECURITY_AUTH_ENABLED"] = bool(security_enabled)
    configured_csrf = app.config.get("CSRF_ENABLED")
    app.config["CSRF_ENABLED"] = (
        app.config["SECURITY_AUTH_ENABLED"] if configured_csrf is None else bool(configured_csrf)
    )

    if app.config["SECURITY_AUTH_ENABLED"]:
        if users_repository is None:
            from app.repositories.users import UsersRepository

            users_repository = UsersRepository(engine)
        if audit_service is None:
            from app.repositories.audit import AuditRepository
            from app.services.audit import AuditService

            audit_service = AuditService(AuditRepository(engine), app_version=app.config["VERSION"])
        app.extensions["users_repository"] = users_repository
        app.extensions["audit_service"] = audit_service
        from app.security.auth import LoginRateLimiter

        app.extensions["login_rate_limiter"] = LoginRateLimiter(
            str(app.config["SECRET_KEY"]),
            attempts=int(app.config["LOGIN_RATE_LIMIT_ATTEMPTS"]),
            window_seconds=int(app.config["LOGIN_RATE_LIMIT_WINDOW_SECONDS"]),
            max_entries=int(app.config["LOGIN_RATE_LIMIT_MAX_ENTRIES"]),
        )

    file_service = app.config.get("FILE_SERVICE")
    if file_service is None and engine is not None and audit_service is not None:
        import sqlalchemy as sa

        inspector = sa.inspect(engine)
        if all(inspector.has_table(name) for name in ("stored_files", "stored_file_versions", "object_files")):
            from app.repositories.files import FilesRepository
            from app.services.files import FileService

            file_service = FileService(
                FilesRepository(engine),
                audit_service,
                storage_root=app.config["FILE_STORAGE_ROOT"],
                max_bytes=int(app.config["FILE_MAX_BYTES"]),
                preview_max_bytes=int(app.config["FILE_PREVIEW_MAX_BYTES"]),
            )
    if file_service is not None:
        app.extensions["file_service"] = file_service

    proposal_service = app.config.get("PROPOSAL_SERVICE")
    if proposal_service is None and engine is not None and audit_service is not None:
        import sqlalchemy as sa

        inspector = sa.inspect(engine)
        if all(inspector.has_table(name) for name in (
            "proposals", "proposal_argumentations", "proposal_decisions"
        )):
            from app.repositories.proposals import ProposalsRepository
            from app.services.proposals import ProposalService

            proposal_service = ProposalService(ProposalsRepository(engine), audit_service)
    if proposal_service is not None:
        app.extensions["proposal_service"] = proposal_service

    assistant_service = app.config.get("ASSISTANT_SERVICE")
    if assistant_service is None and engine is not None and audit_service is not None:
        import sqlalchemy as sa

        provider_kind = str(app.config.get("AI_PROVIDER", "DISABLED")).upper()
        inspector = sa.inspect(engine)
        if provider_kind != "DISABLED" and inspector.has_table("proposal_ai_drafts"):
            if provider_kind == "DEEPSEEK":
                from app.ai.deepseek import DeepSeekProposalAssistant

                provider = DeepSeekProposalAssistant(
                    api_key=app.config.get("DEEPSEEK_API_KEY"),
                    model=app.config.get("DEEPSEEK_MODEL"),
                )
            else:
                from app.ai.local_model import LocalProposalAssistant

                provider = LocalProposalAssistant(
                    base_url=app.config.get("LOCAL_MODEL_BASE_URL"),
                    model=app.config.get("LOCAL_MODEL_NAME"),
                )
            from app.repositories.assistant import AssistantRepository
            from app.services.assistant import AssistantService

            assistant_service = AssistantService(
                AssistantRepository(engine), audit_service, provider,
                file_service=file_service,
            )
    if assistant_service is not None:
        app.extensions["assistant_service"] = assistant_service

    if app.config.get("LOG_FILE"):
        from app.security.logging import configure_json_logging

        configure_json_logging(app)

    from app.routes import api, auth, projects, equipment, standards, users, templates
    from app.routes import crypto_projects, security_projects, crypto_logs, security_logs
    from app.routes import experts, expert_groups, equipment_groups
    from app.routes import utils, expense, documents, host_devices, research_units
    from app.routes.generic_tables import bp as generic_tables_bp
    from app.routes.generic_tables import bp2 as generic_tables_api_bp
    from app.routes.preview import bp as preview_bp
    from app.routes.argumentation import argumentation_bp
    from app.routes.argumentation.template_routes import template_bp
    from app.web.files import bp as files_bp
    from app.web.proposals import bp as proposals_bp
    from app.web.assistant import bp as assistant_bp

    for blueprint in (
        api.bp, auth.bp, projects.bp, equipment.bp, standards.bp, users.bp,
        templates.bp, security_projects.bp, crypto_projects.bp, crypto_logs.bp,
        security_logs.bp, experts.bp, expert_groups.bp, equipment_groups.bp,
        preview_bp, utils.bp, expense.bp, documents.bp, host_devices.bp,
        research_units.bp, generic_tables_bp, generic_tables_api_bp,
        files_bp,
        proposals_bp,
        assistant_bp,
    ):
        app.register_blueprint(blueprint)
    app.register_blueprint(argumentation_bp, url_prefix="/argumentation")
    app.register_blueprint(template_bp)

    @app.route("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.before_request
    def establish_request_context():
        request.request_id = f"req_{uuid.uuid4().hex}"
        request.started_at = time.monotonic()

    @app.before_request
    def verify_csrf():
        from app.security.csrf import protect_request

        return protect_request()

    @app.before_request
    def require_authenticated_user():
        if request.endpoint in PUBLIC_ENDPOINTS:
            return None
        if app.config["SECURITY_AUTH_ENABLED"]:
            from app.security.auth import current_identity

            if current_identity() is None:
                if request.path.startswith("/api/"):
                    return jsonify({"error": {"code": "AUTH_REQUIRED", "message": "请先登录", "requestId": request.request_id}}), 401
                return redirect(url_for("auth.login"))
        elif "user" not in session:
            return redirect(url_for("auth.login"))
        return None

    @app.before_request
    def require_initial_password_change():
        if (
            app.config["SECURITY_AUTH_ENABLED"]
            and session.get("must_change_password")
            and request.endpoint not in PASSWORD_CHANGE_ENDPOINTS
        ):
            return redirect(url_for("users.change_password"))
        return None

    @app.after_request
    def log_request(response):
        if app.config.get("LOG_FILE"):
            duration = max(0, int((time.monotonic() - getattr(request, "started_at", time.monotonic())) * 1000))
            app.logger.info(
                "request completed",
                extra={
                    "request_id": getattr(request, "request_id", None),
                    "user_id": session.get("user_id"),
                    "app_module": request.blueprint or "application",
                    "action": request.endpoint or "unmatched_route",
                    "object_id": None,
                    "duration_ms": duration,
                    "result": "SUCCESS" if response.status_code < 400 else "FAILURE",
                    "error_code": None if response.status_code < 400 else f"HTTP_{response.status_code}",
                },
            )
        if getattr(request, "request_id", None):
            response.headers["X-Request-ID"] = request.request_id
        return response

    @app.context_processor
    def security_context():
        from app.security.csrf import csrf_token

        return {"csrf_token": csrf_token}

    @app.route("/")
    def index():
        from flask import render_template
        from app.models import DIRECTORIES

        return render_template("index.html", visible_directories=DIRECTORIES)

    return app
