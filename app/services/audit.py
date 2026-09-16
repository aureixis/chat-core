import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import (
    AuditChainHead,
    AuditEvent,
    AuditOutbox,
    BotProfile,
    OrganizationMembership,
)
from ..request_context import get_request_context


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def record_audit(
    database: Session,
    action: str,
    entity_type: str,
    entity_id: int | str | None = None,
    *,
    actor_id: int | None = None,
    actor_type: str = "system",
    request_id: str | None = None,
    ip_address: str | None = None,
    metadata: dict[str, Any] | None = None,
    organization_id: int | None = None,
) -> AuditEvent:
    context = get_request_context()
    request_id = request_id or context.request_id
    ip_address = ip_address or context.ip_address
    if organization_id is None and actor_id is not None:
        profile = database.get(BotProfile, actor_id)
        if profile:
            organization_id = profile.organization_id
        else:
            organization_id = database.scalar(
                select(OrganizationMembership.organization_id)
                .where(OrganizationMembership.user_id == actor_id)
                .order_by(OrganizationMembership.id)
                .limit(1)
            )
    query = select(AuditChainHead).where(AuditChainHead.id == 1)
    if database.bind and database.bind.dialect.name == "postgresql":
        query = query.with_for_update()
    head = database.scalar(query)
    if not head:
        head = AuditChainHead(id=1, last_hash="0" * 64)
        database.add(head)
        database.flush()
    created_at = datetime.now(timezone.utc)
    previous_hash = head.last_hash
    metadata_json = metadata or {}
    canonical = json.dumps(
        {
            "organization_id": organization_id,
            "actor_id": actor_id,
            "actor_type": actor_type,
            "action": action,
            "entity_type": entity_type,
            "entity_id": str(entity_id) if entity_id is not None else None,
            "request_id": request_id,
            "ip_address": ip_address,
            "metadata": metadata_json,
            "created_at": _timestamp(created_at),
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    event_hash = hashlib.sha256(f"{previous_hash}:{canonical}".encode()).hexdigest()
    event = AuditEvent(
        organization_id=organization_id,
        actor_id=actor_id,
        actor_type=actor_type,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        request_id=request_id,
        ip_address=ip_address,
        metadata_json=metadata_json,
        previous_hash=previous_hash,
        event_hash=event_hash,
        canonical_payload=canonical,
        created_at=created_at,
    )
    database.add(event)
    database.flush()
    if get_settings().audit_export_url:
        database.add(AuditOutbox(event_id=event.id))
    head.last_hash = event_hash
    return event


def verify_audit_chain(database: Session) -> tuple[bool, int | None]:
    previous_hash = "0" * 64
    rows = list(database.scalars(select(AuditEvent).order_by(AuditEvent.id)))
    for row in rows:
        if not row.event_hash:
            # Legacy events predate hash chaining and form a boundary.
            previous_hash = row.previous_hash or previous_hash
            continue
        canonical = row.canonical_payload or json.dumps(
            {
                "organization_id": row.organization_id,
                "actor_id": row.actor_id,
                "actor_type": row.actor_type,
                "action": row.action,
                "entity_type": row.entity_type,
                "entity_id": row.entity_id,
                "request_id": row.request_id,
                "ip_address": row.ip_address,
                "metadata": row.metadata_json,
                "created_at": _timestamp(row.created_at),
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        if row.canonical_payload:
            try:
                snapshot = json.loads(row.canonical_payload)
            except (TypeError, json.JSONDecodeError):
                return False, row.id
            stable_values = {
                "actor_type": row.actor_type,
                "action": row.action,
                "entity_type": row.entity_type,
                "entity_id": row.entity_id,
                "request_id": row.request_id,
                "ip_address": row.ip_address,
                "metadata": row.metadata_json,
            }
            if any(snapshot.get(key) != value for key, value in stable_values.items()):
                return False, row.id
            # Privacy deletion may null foreign-key projections; any non-null value
            # must still match the immutable subject captured in the snapshot.
            if row.actor_id is not None and snapshot.get("actor_id") != row.actor_id:
                return False, row.id
            if (
                row.organization_id is not None
                and snapshot.get("organization_id") != row.organization_id
            ):
                return False, row.id
        expected = hashlib.sha256(f"{previous_hash}:{canonical}".encode()).hexdigest()
        if row.previous_hash != previous_hash or row.event_hash != expected:
            return False, row.id
        previous_hash = row.event_hash
    return True, None
