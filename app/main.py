import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import delete, or_, select, text
from sqlalchemy.orm import Session

from .config import get_settings
from .database import Base, SessionLocal, engine, get_db
from .models import Connection, Conversation, Message, TranslationSettings, User
from .schemas import (AuthResponse, ConnectionCreate, ConnectionResponse, ConversationResponse, GuestLogin,
                      LoginRequest, MessageResponse, TranslationSettingsResponse, TranslationSettingsUpdate,
                      UserCreate, UserResponse)
from .security import admin_user, create_access_token, current_user, get_user_from_token, hash_password, verify_password
from .translation import DEFAULT_SYSTEM_PROMPT, TranslationService


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    if engine.dialect.name == "postgresql":
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS bio TEXT"))
            connection.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS profile_picture_url VARCHAR(1000)"))
            connection.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS sex VARCHAR(30)"))
            connection.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_guest BOOLEAN NOT NULL DEFAULT FALSE"))
            connection.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS guest_expires_at TIMESTAMPTZ"))
            connection.execute(text("UPDATE users SET email = regexp_replace(email, '@guest\\.lamya\\.local$', '@guest.lamya.app') WHERE email LIKE '%@guest.lamya.local'"))
            connection.execute(text("ALTER TABLE translation_settings ADD COLUMN IF NOT EXISTS system_prompt TEXT"))
    database = SessionLocal()
    try:
        clear_expired_guests(database)
        settings = get_settings()
        admin = database.scalar(select(User).where(User.email == settings.admin_email))
        if not admin:
            database.add(User(email=settings.admin_email, name="Administrator", password_hash=hash_password(settings.admin_password), is_admin=True))
            database.commit()
    finally:
        database.close()
    cleanup_task = asyncio.create_task(expired_guest_cleanup_loop())
    try:
        yield
    finally:
        cleanup_task.cancel()
        await asyncio.gather(cleanup_task, return_exceptions=True)


app = FastAPI(title="Lamya API", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=get_settings().allowed_origins, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


def user_response(user: User) -> UserResponse:
    return UserResponse.model_validate(user)


def clear_expired_guests(database: Session) -> None:
    expired_ids = list(database.scalars(select(User.id).where(User.is_guest.is_(True), User.guest_expires_at <= datetime.now(timezone.utc))))
    if not expired_ids:
        return
    conversation_ids = list(database.scalars(select(Conversation.id).where(or_(Conversation.user_a_id.in_(expired_ids), Conversation.user_b_id.in_(expired_ids)))))
    if conversation_ids:
        database.execute(delete(Message).where(Message.conversation_id.in_(conversation_ids)))
        database.execute(delete(Conversation).where(Conversation.id.in_(conversation_ids)))
    database.execute(delete(Connection).where(or_(Connection.requester_id.in_(expired_ids), Connection.recipient_id.in_(expired_ids))))
    database.execute(delete(User).where(User.id.in_(expired_ids)))
    database.commit()


async def expired_guest_cleanup_loop() -> None:
    while True:
        database = SessionLocal()
        try:
            clear_expired_guests(database)
        finally:
            database.close()
        await asyncio.sleep(3600)


def get_connection(database: Session, first_id: int, second_id: int) -> Connection | None:
    return database.scalar(select(Connection).where(or_(Connection.requester_id == first_id, Connection.recipient_id == first_id), or_(Connection.requester_id == second_id, Connection.recipient_id == second_id), Connection.status == "accepted"))


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/auth/signup", response_model=AuthResponse, status_code=201)
def signup(payload: UserCreate, database: Session = Depends(get_db)):
    if database.scalar(select(User).where(User.email == payload.email.lower())):
        raise HTTPException(status_code=409, detail="Email is already registered")
    user = User(email=payload.email.lower(), name=payload.name.strip(), bio=payload.bio.strip() if payload.bio else None, profile_picture_url=str(payload.profile_picture_url) if payload.profile_picture_url else None, sex=payload.sex, password_hash=hash_password(payload.password), preferred_language=payload.preferred_language)
    database.add(user)
    database.commit()
    database.refresh(user)
    return AuthResponse(access_token=create_access_token(user.id), user=user_response(user))


@app.post("/api/auth/login", response_model=AuthResponse)
def login(payload: LoginRequest, database: Session = Depends(get_db)):
    user = database.scalar(select(User).where(User.email == payload.email.lower()))
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    return AuthResponse(access_token=create_access_token(user.id), user=user_response(user))


@app.post("/api/auth/guest", response_model=AuthResponse, status_code=201)
def guest_login(payload: GuestLogin, database: Session = Depends(get_db)):
    clear_expired_guests(database)
    nickname = payload.nickname.strip()
    if database.scalar(select(User).where(User.is_guest.is_(True), User.name.ilike(nickname))):
        raise HTTPException(status_code=409, detail="That guest nickname is already in use")
    guest_email = f"guest-{uuid4().hex}@guest.lamya.app"
    user = User(email=guest_email, name=nickname, sex=payload.sex, is_guest=True, guest_expires_at=datetime.now(timezone.utc) + timedelta(hours=24), password_hash=hash_password(uuid4().hex), preferred_language=payload.preferred_language)
    database.add(user)
    database.commit()
    database.refresh(user)
    return AuthResponse(access_token=create_access_token(user.id), user=user_response(user))


@app.get("/api/auth/me", response_model=UserResponse)
def me(user: User = Depends(current_user)):
    return user


@app.get("/api/users", response_model=list[UserResponse])
def users(q: str = Query(default="", max_length=120), user: User = Depends(current_user), database: Session = Depends(get_db)):
    pattern = f"%{q.strip()}%"
    return list(database.scalars(select(User).where(User.id != user.id, or_(User.name.ilike(pattern), User.email.ilike(pattern))).order_by(User.name).limit(50)))


@app.get("/api/admin/users", response_model=list[UserResponse])
def admin_users(admin: User = Depends(admin_user), database: Session = Depends(get_db)):
    clear_expired_guests(database)
    return list(database.scalars(select(User).where(User.id != admin.id).order_by(User.name)))


@app.get("/api/connections", response_model=list[ConnectionResponse])
def connections(user: User = Depends(current_user), database: Session = Depends(get_db)):
    rows = list(database.scalars(select(Connection).where(or_(Connection.requester_id == user.id, Connection.recipient_id == user.id)).order_by(Connection.created_at.desc())))
    return [ConnectionResponse.model_validate(row) for row in rows]


@app.post("/api/connections", response_model=ConnectionResponse, status_code=201)
def create_connection(payload: ConnectionCreate, user: User = Depends(current_user), database: Session = Depends(get_db)):
    if payload.recipient_id == user.id or not database.get(User, payload.recipient_id):
        raise HTTPException(status_code=404, detail="Recipient not found")
    existing = database.scalar(select(Connection).where(or_(Connection.requester_id == user.id, Connection.recipient_id == user.id), or_(Connection.requester_id == payload.recipient_id, Connection.recipient_id == payload.recipient_id)))
    if existing:
        raise HTTPException(status_code=409, detail="A connection already exists")
    row = Connection(requester_id=user.id, recipient_id=payload.recipient_id)
    database.add(row)
    database.commit()
    database.refresh(row)
    return row


@app.patch("/api/connections/{connection_id}", response_model=ConnectionResponse)
def update_connection(connection_id: int, status_value: str = Query(alias="status", pattern="^(accepted|rejected)$"), user: User = Depends(current_user), database: Session = Depends(get_db)):
    row = database.get(Connection, connection_id)
    if not row or row.recipient_id != user.id:
        raise HTTPException(status_code=404, detail="Connection request not found")
    row.status = status_value
    database.commit()
    database.refresh(row)
    if status_value == "accepted":
        first, second = sorted((row.requester_id, row.recipient_id))
        if not database.scalar(select(Conversation).where(Conversation.user_a_id == first, Conversation.user_b_id == second)):
            database.add(Conversation(user_a_id=first, user_b_id=second))
            database.commit()
    return row


@app.get("/api/chat/conversations", response_model=list[ConversationResponse])
def conversations(user: User = Depends(current_user), database: Session = Depends(get_db)):
    rows = list(database.scalars(select(Conversation).where(or_(Conversation.user_a_id == user.id, Conversation.user_b_id == user.id))))
    result = []
    for row in rows:
        peer = database.get(User, row.user_b_id if row.user_a_id == user.id else row.user_a_id)
        result.append(ConversationResponse(id=row.id, peer=user_response(peer)))
    return result


@app.get("/api/chat/{conversation_id}/messages", response_model=list[MessageResponse])
def messages(conversation_id: int, user: User = Depends(current_user), database: Session = Depends(get_db)):
    conversation = database.get(Conversation, conversation_id)
    if not conversation or user.id not in (conversation.user_a_id, conversation.user_b_id):
        raise HTTPException(status_code=404, detail="Conversation not found")
    return list(database.scalars(select(Message).where(Message.conversation_id == conversation_id).order_by(Message.created_at)))


@app.get("/api/admin/translation", response_model=TranslationSettingsResponse)
def translation_settings(_: User = Depends(admin_user), database: Session = Depends(get_db)):
    row = database.get(TranslationSettings, 1)
    settings = get_settings()
    if not row:
        return TranslationSettingsResponse(provider=settings.translation_provider, api_url=settings.translation_api_url, model=settings.translation_model, system_prompt=DEFAULT_SYSTEM_PROMPT, has_api_key=bool(settings.translation_api_key))
    return TranslationSettingsResponse(provider=row.provider, api_url=row.api_url, model=row.model, system_prompt=row.system_prompt or DEFAULT_SYSTEM_PROMPT, has_api_key=bool(row.api_key))


@app.put("/api/admin/translation", response_model=TranslationSettingsResponse)
def update_translation(payload: TranslationSettingsUpdate, _: User = Depends(admin_user), database: Session = Depends(get_db)):
    row = database.get(TranslationSettings, 1) or TranslationSettings(id=1)
    row.provider, row.api_url, row.model, row.system_prompt = payload.provider, payload.api_url, payload.model, payload.system_prompt.strip()
    if payload.api_key is not None:
        row.api_key = payload.api_key
    database.add(row)
    database.commit()
    return TranslationSettingsResponse(provider=row.provider, api_url=row.api_url, model=row.model, system_prompt=row.system_prompt, has_api_key=bool(row.api_key))


class ConnectionManager:
    def __init__(self):
        self.connections: dict[int, set[WebSocket]] = {}

    async def connect(self, conversation_id: int, websocket: WebSocket):
        await websocket.accept()
        self.connections.setdefault(conversation_id, set()).add(websocket)

    def disconnect(self, conversation_id: int, websocket: WebSocket):
        self.connections.get(conversation_id, set()).discard(websocket)

    async def broadcast(self, conversation_id: int, payload: dict):
        for websocket in list(self.connections.get(conversation_id, set())):
            await websocket.send_json(payload)


manager = ConnectionManager()


class VideoSignalingManager:
    def __init__(self):
        self.sessions: dict[int, set[WebSocket]] = {}

    async def connect(self, user_id: int, websocket: WebSocket):
        await websocket.accept()
        self.sessions.setdefault(user_id, set()).add(websocket)

    def disconnect(self, user_id: int, websocket: WebSocket):
        self.sessions.get(user_id, set()).discard(websocket)

    async def relay(self, user_id: int, sender: WebSocket, payload: dict):
        for websocket in list(self.sessions.get(user_id, set())):
            if websocket is not sender:
                await websocket.send_json(payload)


video_manager = VideoSignalingManager()


@app.websocket("/ws/{conversation_id}")
async def websocket_chat(websocket: WebSocket, conversation_id: int, token: str,):
    database = SessionLocal()
    try:
        user = get_user_from_token(token, database)
        conversation = database.get(Conversation, conversation_id)
        if not conversation or user.id not in (conversation.user_a_id, conversation.user_b_id):
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        await manager.connect(conversation_id, websocket)
        while True:
            payload = await websocket.receive_json()
            text = str(payload.get("text", "")).strip()
            if not text or len(text) > 4000:
                continue
            source_language = str(payload.get("source_language", user.preferred_language))
            peer_id = conversation.user_b_id if conversation.user_a_id == user.id else conversation.user_a_id
            peer = database.get(User, peer_id)
            translated = TranslationService(database).translate(text, source_language, peer.preferred_language)
            message = Message(conversation_id=conversation_id, sender_id=user.id, original_text=text, source_language=source_language, translated_text=translated, target_language=peer.preferred_language)
            database.add(message)
            database.commit()
            database.refresh(message)
            await manager.broadcast(conversation_id, {"type": "message", "message": MessageResponse.model_validate(message).model_dump(mode="json")})
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(conversation_id, websocket)
        database.close()


@app.websocket("/ws/video/{target_user_id}")
async def websocket_video(websocket: WebSocket, target_user_id: int, token: str, role: str = "user"):
    database = SessionLocal()
    try:
        user = get_user_from_token(token, database)
        if role == "admin":
            if not user.is_admin:
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                return
        elif role != "user" or user.id != target_user_id:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        await video_manager.connect(target_user_id, websocket)
        while True:
            payload = await websocket.receive_json()
            await video_manager.relay(target_user_id, websocket, payload)
    except WebSocketDisconnect:
        pass
    finally:
        video_manager.disconnect(target_user_id, websocket)
        database.close()
