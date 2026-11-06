"""Admin-approved, one-to-one audio/video calls, isolated from screen sharing."""

import asyncio
import json
import secrets
from contextlib import suppress
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, Field

from ..config import get_settings
from ..database import SessionLocal
from ..security import get_user_from_token
from ..services.video_invitations import (
    create_invitation,
    invitation_states,
    list_invitations,
    revoke_invitation,
    validate_token,
)
from .chat import _websocket_credentials

router = APIRouter(tags=["video chat"])


class InvitationCheck(BaseModel):
    token: str = Field(min_length=1, max_length=4096)


@router.post("/api/video-chat/invitations/validate")
def check_invitation(payload: InvitationCheck):
    invitation, invite_status = validate_token(payload.token)
    return {
        "valid": invite_status == "active",
        "status": invite_status,
        "server_now": datetime.now(timezone.utc).isoformat(),
        "invitation": {key: invitation[key] for key in ("id", "label", "expires_at")}
        if invitation and invite_status == "active"
        else None,
    }


class CallRoom:
    def __init__(self):
        self.clients: dict[str, WebSocket] = {}
        self.people: dict[str, dict] = {}
        self.pending: dict[str, None] = {}
        self.approved: dict[str, str] = {}  # Visitor session -> approving admin session.
        self.monitor: asyncio.Task | None = None
        self.invite_statuses: dict[str, str] = {}

    def peer_id(self, participant_id: str) -> str | None:
        return self.approved.get(participant_id) or next(
            (visitor for visitor, admin in self.approved.items() if admin == participant_id), None
        )

    def person(self, participant_id: str | None) -> dict | None:
        person = self.people.get(participant_id)
        return {"id": participant_id, "name": person["name"]} if person else None

    async def send(self, participant_id: str, payload: dict):
        websocket = self.clients.get(participant_id)
        if websocket:
            try:
                await websocket.send_json(payload)
            except (RuntimeError, WebSocketDisconnect, OSError):
                pass

    async def broadcast_state(self):
        invitations = list_invitations()
        self.invite_statuses = {row["id"]: row["status"] for row in invitations}
        for participant_id, person in list(self.people.items()):
            peer = self.person(self.peer_id(participant_id))
            is_admin = person["role"] == "admin"
            unlocked = person["unlocked"] if is_admin else False
            await self.send(
                participant_id,
                {
                    "type": "state",
                    "status": "approved"
                    if peer
                    else "locked"
                    if is_admin and not unlocked
                    else "ready"
                    if is_admin
                    else "waiting"
                    if participant_id in self.pending
                    else "idle",
                    "peer": peer,
                    "unlocked": unlocked,
                    "requests": [self.person(visitor) for visitor in self.pending]
                    if is_admin and unlocked
                    else [],
                    "invitations": invitations if is_admin and unlocked else [],
                    "invite_status": person.get("invite_status", "missing"),
                    "invitation": person.get("invitation"),
                },
            )

    async def end_call(self, participant_id: str, detail: str):
        peer = self.peer_id(participant_id)
        person = self.people.get(participant_id)
        if peer and person:
            visitor = peer if person["role"] == "admin" else participant_id
            self.approved.pop(visitor, None)
            await self.send(peer, {"type": "call_ended", "detail": detail})

    async def invalidate_visitor(self, participant_id: str, invite_status: str) -> bool:
        person = self.people.get(participant_id)
        if (
            not person
            or person["role"] != "visitor"
            or person.get("invite_status") == invite_status
        ):
            return False
        person["invite_status"] = invite_status
        self.pending.pop(participant_id, None)
        detail = {
            "expired": "This invitation has expired. Ask the admin for a new link.",
            "revoked": "The admin revoked this invitation. Ask for a new link.",
        }.get(invite_status, "A valid invitation link is required. Ask the admin for a new link.")
        call_detail = {
            "expired": "The visitor's invitation expired. The call has ended.",
            "revoked": "The invitation was revoked. The call has ended.",
        }.get(invite_status, "The call ended because its invitation is no longer valid.")
        await self.end_call(participant_id, call_detail)
        await self.send(
            participant_id, {"type": "invitation", "status": invite_status, "detail": detail}
        )
        return True

    async def valid_invitation(self, participant_id: str) -> bool:
        person = self.people.get(participant_id)
        invitation_id = person.get("invite_id") if person else None
        invite_status = (
            invitation_states({invitation_id}).get(invitation_id, "invalid")
            if invitation_id
            else "missing"
        )
        if invite_status == "active":
            return True
        if not await self.invalidate_visitor(participant_id, invite_status):
            await self.send(
                participant_id,
                {
                    "type": "invitation",
                    "status": invite_status,
                    "detail": "A valid invitation link is required. Ask the admin for a new link.",
                },
            )
        return False

    async def invalidate_expired(self) -> bool:
        people = [
            (key, person.get("invite_id"))
            for key, person in self.people.items()
            if person["role"] == "visitor" and person.get("invite_status") == "active"
        ]
        states = invitation_states({invite_id for _, invite_id in people if invite_id})
        changed = False
        for key, invite_id in people:
            state = states.get(invite_id, "invalid")
            if state != "active":
                changed = await self.invalidate_visitor(key, state) or changed
        return changed

    async def watch_invitations(self):
        while self.clients:
            await asyncio.sleep(1)
            current = {row["id"]: row["status"] for row in list_invitations()}
            changed = await self.invalidate_expired()
            if changed or current != self.invite_statuses:
                await self.broadcast_state()

    def start_monitor(self):
        if self.monitor is None or self.monitor.done():
            self.monitor = asyncio.create_task(self.watch_invitations())

    async def stop_monitor_if_empty(self):
        if not self.clients and self.monitor:
            monitor = self.monitor
            self.monitor = None
            monitor.cancel()
            with suppress(asyncio.CancelledError):
                await monitor


call_room = CallRoom()


@router.websocket("/ws/call/room")
async def video_chat(websocket: WebSocket, role: Literal["visitor", "admin"] = "visitor"):
    participant_id = uuid4().hex
    try:
        credential, subprotocol = _websocket_credentials(websocket, None)
        with SessionLocal() as database:
            user = get_user_from_token(credential, database)
            name = user.name
        await websocket.accept(subprotocol=subprotocol)
        call_room.clients[participant_id] = websocket
        call_room.people[participant_id] = {"name": name, "role": role, "unlocked": False}
        call_room.start_monitor()
        await websocket.send_json({"type": "welcome", "id": participant_id})
        await call_room.broadcast_state()
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
            kind = payload.get("type")
            person = call_room.people[participant_id]
            if kind == "join_invite" and role == "visitor":
                if call_room.peer_id(participant_id):
                    await call_room.send(
                        participant_id,
                        {"type": "error", "detail": "End the call before changing invitations"},
                    )
                    continue
                invitation, invite_status = validate_token(payload.get("token"))
                person["invite_id"] = invitation["id"] if invitation else None
                person["invite_status"] = invite_status
                person["invitation"] = (
                    {key: invitation[key] for key in ("id", "label", "expires_at")}
                    if invitation
                    else None
                )
                call_room.pending.pop(participant_id, None)
                await call_room.send(
                    participant_id,
                    {
                        "type": "invitation",
                        "status": invite_status,
                        "detail": "Invitation accepted"
                        if invite_status == "active"
                        else "This invitation is invalid, expired, or revoked. Ask the admin for a new link.",
                    },
                )
                await call_room.broadcast_state()
            elif kind == "unlock" and role == "admin":
                password = payload.get("password")
                expected = get_settings().video_admin_password
                granted = (
                    isinstance(password, str)
                    and bool(expected)
                    and secrets.compare_digest(password.encode("utf-8"), expected.encode("utf-8"))
                )
                if granted:
                    person["unlocked"] = True
                await call_room.send(
                    participant_id,
                    {
                        "type": "access",
                        "granted": granted,
                        **({} if granted else {"detail": "Incorrect video password"}),
                    },
                )
                await call_room.broadcast_state()
            elif kind in ("create_invite", "revoke_invite") and role == "admin":
                if not person["unlocked"]:
                    await call_room.send(
                        participant_id, {"type": "error", "detail": "Unlock call controls first"}
                    )
                    continue
                if kind == "create_invite":
                    try:
                        invitation = create_invitation(
                            payload.get("label", ""), payload.get("valid_for_minutes", 60)
                        )
                    except ValueError as error:
                        await call_room.send(
                            participant_id, {"type": "error", "detail": str(error)}
                        )
                        continue
                    await call_room.send(
                        participant_id, {"type": "invite_created", "invitation": invitation}
                    )
                else:
                    invitation_id = payload.get("id")
                    if not isinstance(invitation_id, str) or not revoke_invitation(invitation_id):
                        await call_room.send(
                            participant_id, {"type": "error", "detail": "Invitation not found"}
                        )
                        continue
                    await call_room.invalidate_expired()
                await call_room.broadcast_state()
            elif kind == "request" and role == "visitor":
                if not await call_room.valid_invitation(participant_id):
                    await call_room.broadcast_state()
                    continue
                if participant_id not in call_room.approved:
                    call_room.pending[participant_id] = None
                await call_room.broadcast_state()
            elif kind == "cancel" and role == "visitor":
                call_room.pending.pop(participant_id, None)
                await call_room.broadcast_state()
            elif kind in ("allow", "decline") and role == "admin":
                if not person["unlocked"]:
                    await call_room.send(
                        participant_id, {"type": "error", "detail": "Unlock call controls first"}
                    )
                    continue
                target = payload.get("target")
                if not isinstance(target, str) or target not in call_room.pending:
                    await call_room.send(
                        participant_id,
                        {"type": "error", "detail": "Call request is no longer available"},
                    )
                    continue
                if kind == "allow":
                    if not await call_room.valid_invitation(target):
                        await call_room.broadcast_state()
                        continue
                    if call_room.peer_id(participant_id):
                        await call_room.send(
                            participant_id,
                            {
                                "type": "error",
                                "detail": "End your current call before allowing another visitor",
                            },
                        )
                        continue
                    call_room.pending.pop(target, None)
                    call_room.approved[target] = participant_id
                else:
                    call_room.pending.pop(target, None)
                    await call_room.send(
                        target,
                        {
                            "type": "call_declined",
                            "detail": "The admin declined your call request. You can request again.",
                        },
                    )
                await call_room.broadcast_state()
            elif kind == "end":
                await call_room.end_call(participant_id, "The other participant ended the call.")
                await call_room.broadcast_state()
            elif kind == "signal":
                target = payload.get("target")
                signal = payload.get("signal")
                if (
                    not isinstance(target, str)
                    or call_room.peer_id(participant_id) != target
                    or not isinstance(signal, dict)
                    or signal.get("kind") not in ("offer", "answer", "ice")
                ):
                    continue
                if signal["kind"] == "offer" and role != "admin":
                    continue
                if signal["kind"] == "answer" and role != "visitor":
                    continue
                if not await call_room.valid_invitation(
                    participant_id if role == "visitor" else target
                ):
                    await call_room.broadcast_state()
                    continue
                # Approval binds both the sender and recipient; visitors cannot
                # contact each other or initiate calls by sending an SDP offer.
                await call_room.send(
                    target, {"type": "signal", "from": participant_id, "signal": signal}
                )
    except (WebSocketDisconnect, HTTPException):
        try:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        except RuntimeError:
            pass
    finally:
        peer = call_room.peer_id(participant_id)
        person = call_room.people.get(participant_id)
        if peer and person:
            visitor = peer if person["role"] == "admin" else participant_id
            call_room.approved.pop(visitor, None)
        call_room.pending.pop(participant_id, None)
        call_room.clients.pop(participant_id, None)
        call_room.people.pop(participant_id, None)
        # Clear admission/presence synchronously before notifying other sockets:
        # transport cancellation must not leave a stale client or approved call.
        try:
            if peer:
                await call_room.send(
                    peer, {"type": "call_ended", "detail": "The other participant disconnected."}
                )
        finally:
            await call_room.stop_monitor_if_empty()
            await call_room.broadcast_state()
