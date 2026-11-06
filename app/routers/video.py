"""Live, consent-based video discovery and directed WebRTC signalling."""

import asyncio
import json
import secrets
from contextlib import suppress
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect, status

from ..config import get_settings
from ..database import SessionLocal
from ..security import get_user_from_token
from ..services.video_invitations import invitation_states, validate_token
from .chat import _websocket_credentials

router = APIRouter(tags=["video"])


class VideoRoom:
    def __init__(self):
        self.clients: dict[str, WebSocket] = {}
        self.participants: dict[str, dict] = {}
        self.links: set[tuple[str, str]] = set()
        self.invitations: dict[str, str] = {}
        self.monitor: asyncio.Task | None = None

    async def enforce_invitations(self):
        while self.clients:
            await asyncio.sleep(1)
            states = invitation_states(set(self.invitations.values()))
            for participant_id, invitation_id in list(self.invitations.items()):
                invite_status = states.get(invitation_id, "invalid")
                websocket = self.clients.get(participant_id)
                if invite_status != "active" and websocket:
                    # Remove presence before waiting for the transport close so
                    # admins immediately stop media for the revoked visitor.
                    self.clients.pop(participant_id, None)
                    self.participants.pop(participant_id, None)
                    self.invitations.pop(participant_id, None)
                    self.links = {link for link in self.links if participant_id not in link}
                    try:
                        await websocket.send_json({"type": "invitation", "status": invite_status})
                        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                    except (RuntimeError, WebSocketDisconnect, OSError):
                        pass
                    await self.roster()

    def start_monitor(self):
        if self.monitor is None or self.monitor.done():
            self.monitor = asyncio.create_task(self.enforce_invitations())

    async def stop_monitor_if_empty(self):
        if not self.clients and self.monitor:
            self.monitor.cancel()
            with suppress(asyncio.CancelledError):
                await self.monitor
            self.monitor = None

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


async def _authorize_camera(websocket: WebSocket, target_id: str, password: object) -> bool:
    expected = get_settings().video_admin_password
    granted = (
        isinstance(password, str)
        and bool(expected)
        and secrets.compare_digest(password.encode("utf-8"), expected.encode("utf-8"))
    )
    await websocket.send_json(
        {
            "type": "video_access",
            "target": target_id,
            "granted": granted,
            **({} if granted else {"detail": "Incorrect video password"}),
        }
    )
    return granted


@router.websocket("/ws/live/room")
async def video_room(websocket: WebSocket, role: Literal["visitor", "admin"] = "visitor"):
    # The video console uses its own password gate; it does not grant platform
    # administrator privileges or require an account login.
    participant_id = uuid4().hex
    try:
        credential, subprotocol = _websocket_credentials(websocket, None)
        invitation = None
        if role == "visitor":
            protocols = [
                value.strip()
                for value in websocket.headers.get("sec-websocket-protocol", "").split(",")
            ]
            invitation, invite_status = validate_token(protocols[2] if len(protocols) > 2 else None)
            if invite_status != "active":
                raise HTTPException(status_code=403, detail="Valid invitation required")
        # Do not retain a database connection for the lifetime of a video visit.
        with SessionLocal() as database:
            user = get_user_from_token(credential, database)
            name = user.name
        await websocket.accept(subprotocol=subprotocol)
        room.clients[participant_id] = websocket
        if invitation:
            room.invitations[participant_id] = invitation["id"]
        room.start_monitor()
        room.participants[participant_id] = {
            "id": participant_id,
            "name": name,
            "camera": False,
            "microphone": False,
            "role": role,
        }
        await websocket.send_json({"type": "welcome", "id": participant_id})
        await room.roster()
        while True:
            raw = await websocket.receive_text()
            if participant_id not in room.participants:
                break
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
                updated = False
                for field in ("camera", "microphone"):
                    if isinstance(payload.get(field), bool):
                        room.participants[participant_id][field] = payload[field]
                        updated = True
                if updated:
                    await room.roster()
            elif payload.get("type") == "signal":
                target_id = payload.get("target")
                signal = payload.get("signal")
                if (
                    not isinstance(target_id, str)
                    or target_id == participant_id
                    or not isinstance(signal, dict)
                    or not isinstance(signal.get("kind"), str)
                    or signal.get("kind")
                    not in {
                        "connect",
                        "offer",
                        "answer",
                        "ice",
                        "hangup",
                        "screen",
                        "camera",
                        "microphone",
                    }
                ):
                    continue
                target = room.clients.get(target_id)
                target_participant = room.participants.get(target_id)
                if not target or not target_participant or target_participant["role"] == role:
                    continue
                link = (
                    (participant_id, target_id) if role == "admin" else (target_id, participant_id)
                )
                kind = signal["kind"]
                if kind == "offer":
                    if role != "admin":
                        continue
                    camera_allowed = False
                    microphone_allowed = False
                    if signal.get("purpose") != "screen":
                        microphone_allowed = await _authorize_camera(
                            websocket, target_id, signal.get("password")
                        )
                        if not microphone_allowed:
                            continue
                        camera_allowed = signal.get("purpose") != "audio"
                    room.links.add(link)
                    await room.roster()
                elif link not in room.links:
                    continue
                if kind == "connect" or (kind == "answer" and role != "visitor"):
                    continue
                if kind == "screen" and role != "admin":
                    continue
                if kind in {"camera", "microphone"}:
                    if role != "admin":
                        continue
                    if not (
                        kind == "microphone" and signal.get("enabled") is False
                    ) and not await _authorize_camera(websocket, target_id, signal.get("password")):
                        continue
                forwarded = {key: value for key, value in signal.items() if key != "password"}
                if kind == "offer":
                    # Never trust client-supplied media permissions. Screen-only
                    # connections must not transmit visitor camera or microphone tracks.
                    forwarded["camera_allowed"] = camera_allowed
                    forwarded["microphone_allowed"] = microphone_allowed
                elif kind == "camera":
                    forwarded = {"kind": "camera", "enabled": True}
                elif kind == "microphone":
                    forwarded = {
                        "kind": "microphone",
                        "enabled": signal.get("enabled") is not False,
                    }
                if target:
                    try:
                        # The sender is supplied by the server, never trusted from the client.
                        await target.send_json(
                            {
                                "type": "signal",
                                "from": participant_id,
                                "signal": forwarded,
                            }
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
        room.invitations.pop(participant_id, None)
        room.links = {link for link in room.links if participant_id not in link}
        await room.stop_monitor_if_empty()
        await room.roster()
