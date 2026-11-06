from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from uuid import uuid4

import jwt
from sqlalchemy import select

from ..config import get_settings
from ..database import SessionLocal
from ..models import VideoChatInvitation


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def invitation_status(row: VideoChatInvitation | None) -> str:
    if row is None:
        return "invalid"
    if row.revoked_at is not None:
        return "revoked"
    expires = as_utc(row.expires_at)
    return "expired" if expires and expires <= datetime.now(timezone.utc) else "active"


def invitation_token(row: VideoChatInvitation) -> str:
    settings = get_settings()
    # A separately scoped, signed token is reproducible for Copy Link after a
    # restart. Database expiry/revocation remain authoritative on every use.
    return jwt.encode(
        {"sub": row.id, "iss": settings.jwt_issuer, "type": "video_chat_invite"},
        settings.jwt_secret,
        algorithm="HS256",
    )


def present_invitation(row: VideoChatInvitation) -> dict:
    return {
        "id": row.id,
        "label": row.label,
        "created_at": as_utc(row.created_at).isoformat(),
        "expires_at": as_utc(row.expires_at).isoformat() if row.expires_at else None,
        "status": invitation_status(row),
        "join_path": "/video-chat?" + urlencode({"invite": invitation_token(row)}),
    }


def list_invitations() -> list[dict]:
    with SessionLocal() as database:
        rows = database.scalars(
            select(VideoChatInvitation).order_by(
                VideoChatInvitation.created_at.desc(), VideoChatInvitation.id.desc()
            )
        )
        return [present_invitation(row) for row in rows]


def create_invitation(label: object, minutes: object) -> dict:
    if not isinstance(label, str) or len(label.strip()) > 120:
        raise ValueError("Invitation label must be at most 120 characters")
    if minutes is not None and (
        isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= 525600
    ):
        raise ValueError("Choose 1 to 525600 minutes, or no time limit")
    with SessionLocal() as database:
        now = datetime.now(timezone.utc)
        row = VideoChatInvitation(
            id=uuid4().hex,
            label=label.strip() or "Video chat invitation",
            created_at=now,
            expires_at=now + timedelta(minutes=minutes) if minutes is not None else None,
        )
        database.add(row)
        database.commit()
        database.refresh(row)
        return present_invitation(row)


def revoke_invitation(invitation_id: str) -> bool:
    with SessionLocal() as database:
        row = database.get(VideoChatInvitation, invitation_id)
        if not row:
            return False
        row.revoked_at = row.revoked_at or datetime.now(timezone.utc)
        database.commit()
        return True


def validate_token(token: object) -> tuple[dict | None, str]:
    if not isinstance(token, str) or len(token) > 4096:
        return None, "invalid"
    settings = get_settings()
    try:
        payload = jwt.decode(
            token, settings.jwt_secret, algorithms=["HS256"], issuer=settings.jwt_issuer
        )
        if payload.get("type") != "video_chat_invite" or not isinstance(payload.get("sub"), str):
            return None, "invalid"
    except jwt.PyJWTError:
        return None, "invalid"
    with SessionLocal() as database:
        row = database.get(VideoChatInvitation, payload["sub"])
        return (present_invitation(row) if row else None), invitation_status(row)


def invitation_states(invitation_ids: set[str]) -> dict[str, str]:
    if not invitation_ids:
        return {}
    with SessionLocal() as database:
        rows = database.scalars(
            select(VideoChatInvitation).where(VideoChatInvitation.id.in_(invitation_ids))
        )
        return {row.id: invitation_status(row) for row in rows}
