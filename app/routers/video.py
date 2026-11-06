"""Live, consent-based video discovery and directed WebRTC signalling."""

import json
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect, status

from ..database import SessionLocal
from ..security import get_user_from_token
from .chat import _websocket_credentials

router = APIRouter(tags=["video"])


class VideoRoom:
    def __init__(self):
        self.clients: dict[str, WebSocket] = {}
        self.participants: dict[str, dict] = {}
        self.links: set[tuple[str, str]] = set()

    async def roster(self):
        for participant_id, websocket in list(self.clients.items()):
            participant = self.participants.get(participant_id)
            if not participant:
                continue
            if participant["role"] == "admin":
                visible = [row for row in self.participants.values() if row["role"] == "visitor"]
            else:
                visible = [
                    self.participants[admin_id]
                    for admin_id, visitor_id in self.links
                    if visitor_id == participant_id and admin_id in self.participants
                ]
            try:
                await websocket.send_json({"type": "participants", "participants": visible})
            except (RuntimeError, WebSocketDisconnect, OSError):
                # The receive loop owns removal and the subsequent roster update.
                pass


room = VideoRoom()


@router.websocket("/ws/live/room")
async def video_room(websocket: WebSocket, role: Literal["visitor", "admin"] = "visitor"):
    # This is a no-login presentation role, not the account's administrative
    # privilege. The /admin video console is intentionally available without login.
    participant_id = uuid4().hex
    try:
        credential, subprotocol = _websocket_credentials(websocket, None)
        # Do not retain a database connection for the lifetime of a video visit.
        with SessionLocal() as database:
            user = get_user_from_token(credential, database)
            name = user.name
        await websocket.accept(subprotocol=subprotocol)
        room.clients[participant_id] = websocket
        room.participants[participant_id] = {
            "id": participant_id,
            "name": name,
            "camera": False,
            "role": role,
        }
        await websocket.send_json({"type": "welcome", "id": participant_id})
        await room.roster()
        while True:
            raw = await websocket.receive_text()
            if len(raw) > 65_536:
                await websocket.close(code=status.WS_1009_MESSAGE_TOO_BIG)
                break
            try:
                payload = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            if payload.get("type") == "presence" and role == "visitor":
                if isinstance(payload.get("camera"), bool):
                    room.participants[participant_id]["camera"] = payload["camera"]
                    await room.roster()
            elif payload.get("type") == "signal":
                target_id = payload.get("target")
                signal = payload.get("signal")
                if (
                    not isinstance(target_id, str)
                    or target_id == participant_id
                    or not isinstance(signal, dict)
                    or not isinstance(signal.get("kind"), str)
                    or signal.get("kind") not in {"connect", "offer", "answer", "ice", "hangup", "screen"}
                ):
                    continue
                target = room.clients.get(target_id)
                target_participant = room.participants.get(target_id)
                if not target or not target_participant or target_participant["role"] == role:
                    continue
                link = (participant_id, target_id) if role == "admin" else (target_id, participant_id)
                kind = signal["kind"]
                if kind == "offer":
                    if role != "admin":
                        continue
                    room.links.add(link)
                    await room.roster()
                elif link not in room.links:
                    continue
                if kind == "connect" or (kind == "answer" and role != "visitor"):
                    continue
                if kind == "screen" and role != "admin":
                    continue
                if target:
                    try:
                        # The sender is supplied by the server, never trusted from the client.
                        await target.send_json(
                            {"type": "signal", "from": participant_id, "signal": signal}
                        )
                    except (RuntimeError, WebSocketDisconnect, OSError):
                        pass
                if kind == "hangup":
                    room.links.discard(link)
                    await room.roster()
    except (WebSocketDisconnect, HTTPException):
        try:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        except RuntimeError:
            pass
    finally:
        room.clients.pop(participant_id, None)
        room.participants.pop(participant_id, None)
        room.links = {link for link in room.links if participant_id not in link}
        await room.roster()
