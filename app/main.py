from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select, text

from .config import get_settings
from .database import Base, SessionLocal, engine
from .housekeeping import clear_expired_guests
from .models import (
    AuditChainHead,
    AuditOutbox,
    BotAction,
    KnowledgeChunk,
    ModerationCase,
    PlatformState,
    RobotKnowledgeSource,
    RobotMemory,
    User,
)
from .request_context import reset_request_context, set_request_context
from .routers import admin, auth, chat, connections, privacy, robots, social, users
from .security import hash_password
from .tenancy import apply_worker_context
from .worker import agent_worker_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


async def _guest_cleanup_loop() -> None:
    while True:
        database = SessionLocal()
        try:
            clear_expired_guests(database)
        except Exception:
            database.rollback()
            logger.exception("Guest cleanup failed")
        finally:
            database.close()
        await asyncio.sleep(3600)


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    settings.validate_vector_configuration()
    if settings.is_production:
        if (
            settings.jwt_secret in {"change-me", "replace-this-in-production"}
            or len(settings.jwt_secret) < 32
        ):
            raise RuntimeError(
                "JWT_SECRET must contain at least 32 unpredictable characters in production"
            )
        if settings.admin_password == "change-me-now" or len(settings.admin_password) < 12:
            raise RuntimeError(
                "ADMIN_PASSWORD must be changed and contain at least 12 characters in production"
            )
    if settings.auto_create_schema:
        if engine.dialect.name == "postgresql":
            with engine.begin() as connection:
                connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        Base.metadata.create_all(bind=engine)
    database = SessionLocal()
    try:
        if not database.get(PlatformState, 1):
            database.add(PlatformState(id=1))
        if not database.get(AuditChainHead, 1):
            database.add(AuditChainHead(id=1, last_hash="0" * 64))
        configured_admin = database.scalar(
            select(User).where(User.email == settings.admin_email.lower())
        )
        if not configured_admin:
            database.add(
                User(
                    email=settings.admin_email.lower(),
                    name="Administrator",
                    password_hash=hash_password(settings.admin_password),
                    is_admin=True,
                )
            )
        elif not configured_admin.is_admin:
            logger.warning("Configured ADMIN_EMAIL belongs to a non-admin account")
        database.commit()
    finally:
        database.close()

    tasks = [asyncio.create_task(_guest_cleanup_loop())]
    if settings.agent_worker_enabled:
        tasks.append(asyncio.create_task(agent_worker_loop()))
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(
    title="Lamya API",
    description="Governed autonomous social network and AI companion platform",
    version="2.0.0",
    lifespan=lifespan,
    docs_url=None if get_settings().is_production else "/docs",
    redoc_url=None if get_settings().is_production else "/redoc",
    openapi_url=None if get_settings().is_production else "/openapi.json",
)
settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def platform_middleware(request: Request, call_next):
    supplied_request_id = request.headers.get("X-Request-ID", "")
    request_id = (
        supplied_request_id
        if supplied_request_id
        and len(supplied_request_id) <= 100
        and all(character.isalnum() or character in "-_." for character in supplied_request_id)
        else uuid4().hex
    )
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            request_bytes = int(content_length)
        except ValueError:
            return Response("Invalid Content-Length", status_code=400)
        if request_bytes > settings.max_request_bytes:
            return Response("Request body too large", status_code=413)
    context_token = set_request_context(request_id, request.client.host if request.client else None)
    try:
        response = await call_next(request)
    finally:
        reset_request_context(context_token)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if settings.is_production:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.get("/health")
@app.get("/health/live")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "lamya-api"}


@app.get("/health/ready")
def readiness() -> dict[str, str]:
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    return {"status": "ready", "database": "ok"}


@app.get("/metrics", response_class=PlainTextResponse)
def metrics(request: Request) -> PlainTextResponse:
    configured = settings.metrics_token
    supplied = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if configured and not secrets.compare_digest(supplied, configured):
        return PlainTextResponse("Unauthorized\n", status_code=401)
    database = SessionLocal()
    apply_worker_context(database)
    try:
        queue_counts = dict(
            database.execute(
                select(BotAction.status, func.count()).group_by(BotAction.status)
            ).all()
        )
        oldest = database.scalar(
            select(func.min(BotAction.created_at)).where(BotAction.status == "pending")
        )
        if oldest and oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=timezone.utc)
        queue_age = (
            max(0.0, (datetime.now(timezone.utc) - oldest).total_seconds()) if oldest else 0.0
        )
        failed_vectors = (
            database.scalar(
                select(func.count(RobotMemory.id)).where(RobotMemory.embedding_status == "failed")
            )
            or 0
        ) + (
            database.scalar(
                select(func.count(RobotKnowledgeSource.id)).where(
                    RobotKnowledgeSource.status == "failed"
                )
            )
            or 0
        )
        ready_chunks = (
            database.scalar(
                select(func.count(KnowledgeChunk.id)).where(
                    KnowledgeChunk.embedding_status == "ready"
                )
            )
            or 0
        )
        open_cases = (
            database.scalar(
                select(func.count(ModerationCase.id)).where(
                    ModerationCase.status.in_(["open", "investigating"])
                )
            )
            or 0
        )
        pending_audit_exports = (
            database.scalar(
                select(func.count(AuditOutbox.id)).where(AuditOutbox.status == "pending")
            )
            or 0
        )
        lines = [
            "# TYPE lamya_bot_actions gauge",
            *[
                f'lamya_bot_actions{{status="{status}"}} {count}'
                for status, count in sorted(queue_counts.items())
            ],
            "# TYPE lamya_oldest_pending_action_seconds gauge",
            f"lamya_oldest_pending_action_seconds {queue_age:.3f}",
            "# TYPE lamya_failed_vectors gauge",
            f"lamya_failed_vectors {failed_vectors}",
            "# TYPE lamya_ready_knowledge_chunks gauge",
            f"lamya_ready_knowledge_chunks {ready_chunks}",
            "# TYPE lamya_open_moderation_cases gauge",
            f"lamya_open_moderation_cases {open_cases}",
            "# TYPE lamya_pending_audit_exports gauge",
            f"lamya_pending_audit_exports {pending_audit_exports}",
        ]
        return PlainTextResponse("\n".join(lines) + "\n")
    finally:
        database.close()


app.include_router(auth.router)
app.include_router(users.router)
app.include_router(connections.router)
app.include_router(chat.router)
app.include_router(social.router)
app.include_router(robots.router)
app.include_router(privacy.router)
app.include_router(admin.router)
