from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import SessionLocal, get_db
from ..models import BotProfile, Companion, Conversation, Message, ModerationCase, User
from ..presenters import public_user_response
from ..schemas import ConversationResponse, MessageCreate, MessageResponse, message_response_for
from ..security import current_user, get_user_from_token
from ..services.agents import enqueue_action
from ..services.audit import record_audit
from ..services.moderation import ModerationService
from ..translation import TranslationService

router = APIRouter(tags=["chat"])


def _conversation_for(database: Session, conversation_id: int, user_id: int) -> Conversation:
    conversation = database.get(Conversation, conversation_id)
    if not conversation or user_id not in (conversation.user_a_id, conversation.user_b_id):
        raise HTTPException(status_code=404, detail="Conversation not found")
    if conversation.status != "active":
        raise HTTPException(status_code=409, detail="Conversation is not active")
    return conversation


def _create_message(
    database: Session,
    conversation: Conversation,
    user: User,
    text: str,
    source_language: str,
    client_message_id: str | None = None,
) -> Message:
    if client_message_id:
        existing = database.scalar(
            select(Message).where(
                Message.conversation_id == conversation.id,
                Message.client_message_id == client_message_id,
            )
        )
        if existing:
            return existing
    peer_id = (
        conversation.user_b_id if conversation.user_a_id == user.id else conversation.user_a_id
    )
    peer = database.get(User, peer_id)
    policy = ModerationService(database).evaluate(text, generated=False)
    translated = TranslationService(database).translate(
        text, source_language, peer.preferred_language
    )
    message = Message(
        conversation_id=conversation.id,
        sender_id=user.id,
        original_text=text,
        source_language=source_language,
        translated_text=translated,
        target_language=peer.preferred_language,
        client_message_id=client_message_id,
        moderation_status=policy.status,
    )
    database.add(message)
    database.flush()
    if not policy.allowed:
        database.add(
            ModerationCase(
                entity_type="message",
                entity_id=message.id,
                reporter_id=user.id,
                severity="high",
                category="automated_flag",
                reason=", ".join(policy.reasons),
                evidence={"content": text},
            )
        )
    if peer.is_bot:
        profile = database.get(BotProfile, peer.id)
        if profile and profile.status == "active" and profile.reply_enabled and policy.allowed:
            enqueue_action(
                database,
                peer.id,
                "reply_message",
                target_id=message.id,
                trigger="incoming_message",
                idempotency_key=f"message-reply:{message.id}:{peer.id}",
            )
        companion = database.scalar(
            select(Companion).where(
                Companion.user_id == user.id,
                Companion.bot_id == peer.id,
                Companion.status == "active",
            )
        )
        if companion:
            companion.last_interaction_at = message.created_at
    record_audit(database, "message.sent", "message", message.id, actor_id=user.id)
    database.commit()
    database.refresh(message)
    return message


@router.get("/api/chat/conversations", response_model=list[ConversationResponse])
def conversations(user: User = Depends(current_user), database: Session = Depends(get_db)):
    rows = list(
        database.scalars(
            select(Conversation)
            .where(
                or_(Conversation.user_a_id == user.id, Conversation.user_b_id == user.id),
                Conversation.status == "active",
            )
            .order_by(Conversation.updated_at.desc())
        )
    )
    result = []
    for row in rows:
        peer = database.get(User, row.user_b_id if row.user_a_id == user.id else row.user_a_id)
        result.append(
            ConversationResponse(id=row.id, peer=public_user_response(peer), status=row.status)
        )
    return result


@router.get("/api/chat/{conversation_id}/messages", response_model=list[MessageResponse])
def messages(
    conversation_id: int,
    user: User = Depends(current_user),
    database: Session = Depends(get_db),
):
    _conversation_for(database, conversation_id, user.id)
    rows = list(
        database.scalars(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at, Message.id)
        )
    )
    return [message_response_for(message, user.id) for message in rows]


@router.post(
    "/api/chat/{conversation_id}/messages", response_model=MessageResponse, status_code=201
)
def send_message(
    conversation_id: int,
    payload: MessageCreate,
    user: User = Depends(current_user),
    database: Session = Depends(get_db),
):
    conversation = _conversation_for(database, conversation_id, user.id)
    row = _create_message(
        database,
        conversation,
        user,
        payload.text.strip(),
        payload.source_language,
        payload.client_message_id,
    )
    return message_response_for(row, user.id)


def _websocket_credentials(
    websocket: WebSocket, legacy_query_token: str | None
) -> tuple[str, str | None]:
    origin = websocket.headers.get("origin")
    if origin and origin.rstrip("/") not in get_settings().allowed_origins:
        raise HTTPException(status_code=403, detail="WebSocket origin is not allowed")
    authorization = websocket.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip(), None
    protocols = [
        value.strip() for value in websocket.headers.get("sec-websocket-protocol", "").split(",")
    ]
    if len(protocols) >= 2 and protocols[0] == "lamya-bearer" and protocols[1]:
        return protocols[1], "lamya-bearer"
    if legacy_query_token and not get_settings().is_production:
        return legacy_query_token, None
    raise HTTPException(status_code=401, detail="WebSocket authentication required")


class ConnectionManager:
    def __init__(self):
        self.connections: dict[int, dict[WebSocket, int]] = {}

    async def connect(
        self,
        conversation_id: int,
        websocket: WebSocket,
        user_id: int,
        subprotocol: str | None,
    ):
        await websocket.accept(subprotocol=subprotocol)
        self.connections.setdefault(conversation_id, {})[websocket] = user_id

    def disconnect(self, conversation_id: int, websocket: WebSocket):
        self.connections.get(conversation_id, {}).pop(websocket, None)

    async def broadcast(self, conversation_id: int, payloads: dict[int, dict]):
        for websocket, user_id in list(self.connections.get(conversation_id, {}).items()):
            payload = payloads.get(user_id)
            if payload:
                try:
                    await websocket.send_json(payload)
                except RuntimeError:
                    self.disconnect(conversation_id, websocket)


manager = ConnectionManager()


@router.websocket("/ws/{conversation_id}")
async def websocket_chat(websocket: WebSocket, conversation_id: int, token: str | None = None):
    database = SessionLocal()
    user = None
    try:
        credential, subprotocol = _websocket_credentials(websocket, token)
        user = get_user_from_token(credential, database)
        conversation = _conversation_for(database, conversation_id, user.id)
        peer_id = (
            conversation.user_b_id if conversation.user_a_id == user.id else conversation.user_a_id
        )
        await manager.connect(conversation_id, websocket, user.id, subprotocol)
        while True:
            payload = await websocket.receive_json()
            text = str(payload.get("text", "")).strip()
            if not text or len(text) > 4000:
                continue
            row = _create_message(
                database,
                conversation,
                user,
                text,
                str(payload.get("source_language", user.preferred_language)),
                str(payload["client_message_id"]) if payload.get("client_message_id") else None,
            )
            await manager.broadcast(
                conversation_id,
                {
                    user.id: {
                        "type": "message",
                        "message": message_response_for(row, user.id).model_dump(mode="json"),
                    },
                    peer_id: {
                        "type": "message",
                        "message": message_response_for(row, peer_id).model_dump(mode="json"),
                    },
                },
            )
    except (WebSocketDisconnect, HTTPException):
        if user is None:
            try:
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            except RuntimeError:
                pass
    finally:
        manager.disconnect(conversation_id, websocket)
        database.close()


class VideoSignalingManager:
    def __init__(self):
        self.sessions: dict[int, set[WebSocket]] = {}

    async def connect(self, session_id: int, websocket: WebSocket, subprotocol: str | None):
        await websocket.accept(subprotocol=subprotocol)
        self.sessions.setdefault(session_id, set()).add(websocket)

    def disconnect(self, user_id: int, websocket: WebSocket):
        self.sessions.get(user_id, set()).discard(websocket)

    async def relay(self, user_id: int, sender: WebSocket, payload: dict):
        for websocket in list(self.sessions.get(user_id, set())):
            if websocket is not sender:
                await websocket.send_json(payload)


video_manager = VideoSignalingManager()


@router.websocket("/ws/video/{target_user_id}")
async def websocket_video(websocket: WebSocket, target_user_id: int, token: str | None = None):
    database = SessionLocal()
    session_id = 0
    try:
        credential, subprotocol = _websocket_credentials(websocket, token)
        user = get_user_from_token(credential, database)
        conversation = database.scalar(
            select(Conversation).where(
                Conversation.status == "active",
                or_(
                    (Conversation.user_a_id == user.id)
                    & (Conversation.user_b_id == target_user_id),
                    (Conversation.user_a_id == target_user_id)
                    & (Conversation.user_b_id == user.id),
                ),
            )
        )
        if not conversation:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        first, second = sorted((user.id, target_user_id))
        session_id = (first << 32) | second
        await video_manager.connect(session_id, websocket, subprotocol)
        while True:
            payload = await websocket.receive_json()
            await video_manager.relay(session_id, websocket, payload)
    except (WebSocketDisconnect, HTTPException):
        try:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        except RuntimeError:
            pass
    finally:
        if session_id:
            video_manager.disconnect(session_id, websocket)
        database.close()
