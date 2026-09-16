from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import event, text
from sqlalchemy.orm import Session


@dataclass(frozen=True)
class SecurityContext:
    user_id: int | None = None
    organization_ids: tuple[int, ...] = ()
    organization_write_ids: tuple[int, ...] = ()
    is_admin: bool = False
    is_worker: bool = False


def _set_postgres_context(connection, context: SecurityContext) -> None:
    values = {
        "user_id": str(context.user_id or ""),
        "organization_ids": ",".join(str(value) for value in context.organization_ids),
        "organization_write_ids": ",".join(str(value) for value in context.organization_write_ids),
        "is_admin": "true" if context.is_admin else "false",
        "is_worker": "true" if context.is_worker else "false",
    }
    for key, value in values.items():
        connection.execute(
            text("SELECT set_config(:key, :value, true)"),
            {"key": f"app.{key}", "value": value},
        )


@event.listens_for(Session, "after_begin")
def _restore_context_after_begin(session: Session, transaction, connection) -> None:
    context = session.info.get("security_context")
    if context and connection.dialect.name == "postgresql":
        _set_postgres_context(connection, context)


def apply_security_context(database: Session, context: SecurityContext) -> None:
    database.info["security_context"] = context
    if database.bind and database.bind.dialect.name == "postgresql" and database.in_transaction():
        _set_postgres_context(database.connection(), context)


def apply_worker_context(database: Session) -> None:
    apply_security_context(database, SecurityContext(is_worker=True))
