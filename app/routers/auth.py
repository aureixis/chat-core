from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..housekeeping import clear_expired_guests
from ..models import User
from ..presenters import user_response
from ..schemas import AuthResponse, GuestLogin, LoginRequest, UserCreate, UserResponse
from ..security import (
    create_access_token,
    current_user,
    hash_password,
    verify_login_password,
)
from ..services.audit import record_audit
from ..tenancy import SecurityContext, apply_security_context

router = APIRouter(prefix="/api/auth", tags=["authentication"])


@router.post("/signup", response_model=AuthResponse, status_code=201)
def signup(payload: UserCreate, database: Session = Depends(get_db)):
    email = payload.email.lower()
    if database.scalar(select(User).where(User.email == email)):
        raise HTTPException(status_code=409, detail="Email is already registered")
    user = User(
        email=email,
        name=payload.name.strip(),
        bio=payload.bio.strip() if payload.bio else None,
        profile_picture_url=payload.profile_picture_url,
        sex=payload.sex,
        password_hash=hash_password(payload.password),
        preferred_language=payload.preferred_language,
    )
    database.add(user)
    database.flush()
    apply_security_context(database, SecurityContext(user_id=user.id))
    record_audit(database, "auth.signup", "user", user.id, actor_id=user.id, actor_type="user")
    database.commit()
    database.refresh(user)
    return AuthResponse(access_token=create_access_token(user.id), user=user_response(user))


@router.post("/login", response_model=AuthResponse)
def login(payload: LoginRequest, database: Session = Depends(get_db)):
    user = database.scalar(select(User).where(User.email == payload.email.lower()))
    valid_password = verify_login_password(
        payload.password,
        user.password_hash if user and not user.is_bot else None,
    )
    if not user or user.is_bot or not valid_password:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if user.account_status != "active":
        raise HTTPException(status_code=403, detail="Account is not active")
    apply_security_context(database, SecurityContext(user_id=user.id, is_admin=user.is_admin))
    user.last_login_at = datetime.now(timezone.utc)
    record_audit(database, "auth.login", "user", user.id, actor_id=user.id, actor_type="user")
    database.commit()
    return AuthResponse(access_token=create_access_token(user.id), user=user_response(user))


@router.post("/guest", response_model=AuthResponse, status_code=201)
def guest_login(payload: GuestLogin, database: Session = Depends(get_db)):
    clear_expired_guests(database)
    nickname = payload.nickname.strip()
    if database.scalar(select(User).where(User.is_guest.is_(True), User.name.ilike(nickname))):
        raise HTTPException(status_code=409, detail="That guest nickname is already in use")
    user = User(
        email=f"guest-{uuid4().hex}@guest.lamya.app",
        name=nickname,
        sex=payload.sex,
        is_guest=True,
        guest_expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        password_hash=hash_password(uuid4().hex),
        preferred_language=payload.preferred_language,
    )
    database.add(user)
    database.commit()
    database.refresh(user)
    return AuthResponse(access_token=create_access_token(user.id), user=user_response(user))


@router.get("/me", response_model=UserResponse)
def me(user: User = Depends(current_user)):
    return user_response(user)
