from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from pwdlib import PasswordHash
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .database import get_db
from .models import OrganizationMembership, User
from .tenancy import SecurityContext, apply_security_context

password_hash = PasswordHash.recommended()
_DUMMY_PASSWORD_HASH = password_hash.hash("lamya-dummy-authentication-secret")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    return password_hash.verify(password, hashed)


def verify_login_password(password: str, stored_hash: str | None) -> bool:
    """Run one password verification even when the account does not exist."""
    return verify_password(password, stored_hash or _DUMMY_PASSWORD_HASH)


def create_access_token(user_id: int) -> str:
    settings = get_settings()
    expires = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_minutes)
    return jwt.encode(
        {"sub": str(user_id), "exp": expires, "iss": settings.jwt_issuer, "type": "access"},
        settings.jwt_secret,
        algorithm="HS256",
    )


def get_user_from_token(token: str, database: Session) -> User:
    try:
        settings = get_settings()
        payload = jwt.decode(
            token, settings.jwt_secret, algorithms=["HS256"], issuer=settings.jwt_issuer
        )
        if payload.get("type") != "access":
            raise ValueError("Wrong token type")
        user_id = int(payload["sub"])
    except (jwt.PyJWTError, KeyError, TypeError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authentication token"
        ) from error
    user = database.get(User, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    if user.account_status != "active":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is not active")
    # Establish identity first so the membership table's RLS policy can reveal
    # only this subject's memberships; then enrich the session with org scopes.
    apply_security_context(
        database,
        SecurityContext(user_id=user.id, is_admin=user.is_admin),
    )
    memberships = database.execute(
        select(OrganizationMembership.organization_id, OrganizationMembership.role).where(
            OrganizationMembership.user_id == user.id
        )
    ).all()
    organization_ids = tuple(organization_id for organization_id, _ in memberships)
    organization_write_ids = tuple(
        organization_id
        for organization_id, role in memberships
        if role in {"owner", "admin", "operator"}
    )
    apply_security_context(
        database,
        SecurityContext(
            user_id=user.id,
            organization_ids=organization_ids,
            organization_write_ids=organization_write_ids,
            is_admin=user.is_admin,
        ),
    )
    return user


def current_user(token: str = Depends(oauth2_scheme), database: Session = Depends(get_db)) -> User:
    return get_user_from_token(token, database)


def admin_user(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Administrator access required"
        )
    return user
