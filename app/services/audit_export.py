from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import AuditEvent, AuditOutbox
from .secrets import SecretResolver


def export_pending_audit_events(database: Session) -> int:
    settings = get_settings()
    if not settings.audit_export_url:
        return 0
    now = datetime.now(timezone.utc)
    query = (
        select(AuditOutbox, AuditEvent)
        .join(AuditEvent, AuditEvent.id == AuditOutbox.event_id)
        .where(
            AuditOutbox.status == "pending",
            AuditOutbox.next_attempt_at <= now,
        )
        .order_by(AuditOutbox.id)
        .limit(settings.audit_export_batch_size)
    )
    if database.bind and database.bind.dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True, of=AuditOutbox)
    rows = database.execute(query).all()
    if not rows:
        return 0

    token = None
    if settings.audit_export_secret_ref:
        token = SecretResolver().resolve(None, settings.audit_export_secret_ref)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    payload = {
        "events": [
            {
                "event_id": event.id,
                "event_hash": event.event_hash,
                "previous_hash": event.previous_hash,
                "canonical_payload": event.canonical_payload,
            }
            for _, event in rows
        ]
    }
    for outbox, _ in rows:
        outbox.attempts += 1
    with httpx.Client(timeout=10.0) as client:
        try:
            response = client.post(
                settings.audit_export_url,
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
        except (httpx.HTTPError, ValueError) as error:
            for outbox, _ in rows:
                outbox.last_error = str(error)[:2000]
                outbox.next_attempt_at = now + timedelta(
                    seconds=min(3600, 30 * (2 ** min(outbox.attempts - 1, 7)))
                )
            return 0
        for outbox, _ in rows:
            outbox.status = "exported"
            outbox.exported_at = now
            outbox.last_error = None
    return len(rows)
