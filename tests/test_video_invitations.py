from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest

from app.config import get_settings
from app.database import SessionLocal
from app.models import VideoChatInvitation
from app.routers.video_chat import call_room
from app.services.video_invitations import (
    create_invitation,
    list_invitations,
    revoke_invitation,
    validate_token,
)


def token(invitation):
    return parse_qs(urlsplit(invitation["join_path"]).query)["invite"][0]


def protocols(client, name):
    response = client.post(
        "/api/auth/guest",
        json={"nickname": name, "sex": "prefer_not_to_say", "preferred_language": "en"},
    )
    assert response.status_code == 201
    return ["lamya-bearer", response.json()["access_token"]]


def join(visitor, invitation):
    visitor.send_json({"type": "join_invite", "token": token(invitation)})
    assert visitor.receive_json()["status"] == "active"
    assert visitor.receive_json()["invite_status"] == "active"


def test_unique_links_persist_and_have_configurable_expiry(client):
    timed = create_invitation("Timed", 60)
    manual = create_invitation("Manual", None)
    assert timed["id"] != manual["id"]
    assert timed["join_path"] != manual["join_path"]
    assert timed["expires_at"] is not None
    assert manual["expires_at"] is None
    assert validate_token(token(timed))[1] == "active"
    stored = {row["id"]: row for row in list_invitations()}
    assert stored[timed["id"]]["join_path"] == timed["join_path"]
    assert stored[manual["id"]]["join_path"] == manual["join_path"]
    assert revoke_invitation(timed["id"])
    assert validate_token(token(timed))[1] == "revoked"
    assert validate_token(token(manual))[1] == "active"


@pytest.mark.parametrize("state", ["active", "invalid", "expired", "revoked"])
def test_public_link_check_returns_only_safe_metadata_before_guest_entry(client, state):
    invitation = create_invitation("Public check", None)
    if state == "revoked":
        revoke_invitation(invitation["id"])
    elif state == "expired":
        with SessionLocal() as database:
            row = database.get(VideoChatInvitation, invitation["id"])
            row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            database.commit()
    response = client.post(
        "/api/video-chat/invitations/validate",
        json={"token": "invalid" if state == "invalid" else token(invitation)},
    )
    assert response.status_code == 200
    result = response.json()
    assert result["valid"] is (state == "active")
    assert result["status"] == state
    assert result["server_now"]
    if state == "active":
        assert result["invitation"] == {
            "id": invitation["id"],
            "label": "Public check",
            "expires_at": None,
        }
    else:
        assert result["invitation"] is None


@pytest.mark.parametrize("minutes", [0, -1, True, 525601, "60", 1.5])
def test_invalid_invitation_durations_are_rejected(client, minutes):
    with pytest.raises(ValueError):
        create_invitation("Invalid duration", minutes)
    assert list_invitations() == []


def test_invitation_tokens_are_signed_and_scoped(client):
    invitation = create_invitation("Scoped", 15)
    original = token(invitation)
    header, payload, signature = original.split(".")
    altered = f"{header}.{payload}.{'A' if signature[0] != 'A' else 'B'}{signature[1:]}"
    assert validate_token(altered)[1] == "invalid"
    settings = get_settings()
    wrong_scope = jwt.encode(
        {"sub": invitation["id"], "iss": settings.jwt_issuer, "type": "access"},
        settings.jwt_secret,
        algorithm="HS256",
    )
    assert validate_token(wrong_scope)[1] == "invalid"


@pytest.mark.parametrize("state", ["missing", "invalid", "expired", "revoked"])
def test_visitors_cannot_request_calls_with_unusable_invitations(client, state):
    invitation = create_invitation("Unusable", 60)
    if state == "expired":
        with SessionLocal() as database:
            row = database.get(VideoChatInvitation, invitation["id"])
            row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            database.commit()
    elif state == "revoked":
        revoke_invitation(invitation["id"])
    auth = protocols(client, "Uninvited caller")
    with client.websocket_connect("/ws/call/room", subprotocols=auth) as visitor:
        visitor.receive_json()
        visitor.receive_json()
        if state != "missing":
            visitor.send_json(
                {
                    "type": "join_invite",
                    "token": "invalid" if state == "invalid" else token(invitation),
                }
            )
            assert visitor.receive_json()["status"] == state
            assert visitor.receive_json()["invite_status"] == state
        visitor.send_json({"type": "request"})
        assert visitor.receive_json()["type"] == "invitation"
        assert visitor.receive_json()["peer"] is None
        assert call_room.pending == {}
        assert call_room.approved == {}


def test_admin_link_creation_requires_unlock_and_survives_reconnection(client):
    auth = protocols(client, "Link administrator")
    with client.websocket_connect("/ws/call/room?role=admin", subprotocols=auth) as admin:
        admin.receive_json()
        assert admin.receive_json()["invitations"] == []
        admin.send_json({"type": "create_invite", "label": "Session", "valid_for_minutes": 15})
        assert admin.receive_json()["detail"] == "Unlock call controls first"
        assert list_invitations() == []
        admin.send_json({"type": "unlock", "password": "1234"})
        admin.receive_json()
        admin.receive_json()
        admin.send_json({"type": "create_invite", "label": "Session", "valid_for_minutes": 15})
        invitation = admin.receive_json()["invitation"]
        assert admin.receive_json()["invitations"][0]["id"] == invitation["id"]
    assert call_room.monitor is None
    with client.websocket_connect("/ws/call/room?role=admin", subprotocols=auth) as admin:
        admin.receive_json()
        assert admin.receive_json()["invitations"] == []
        admin.send_json({"type": "unlock", "password": "1234"})
        admin.receive_json()
        persisted = admin.receive_json()["invitations"]
        assert persisted[0]["join_path"] == invitation["join_path"]
        admin.send_json({"type": "revoke_invite", "id": invitation["id"]})
        assert admin.receive_json()["invitations"][0]["status"] == "revoked"
    assert validate_token(token(invitation))[1] == "revoked"


@pytest.mark.parametrize("active_call", [False, True])
def test_revocation_removes_waiting_requests_and_ends_active_calls(client, active_call):
    invitation = create_invitation("Revocable", None)
    visitor_auth = protocols(client, "Revocable visitor")
    admin_auth = protocols(client, "Revoking admin")
    with client.websocket_connect("/ws/call/room", subprotocols=visitor_auth) as visitor:
        visitor_id = visitor.receive_json()["id"]
        visitor.receive_json()
        join(visitor, invitation)
        with client.websocket_connect("/ws/call/room?role=admin", subprotocols=admin_auth) as admin:
            admin.receive_json()
            admin.receive_json()
            visitor.receive_json()
            admin.send_json({"type": "unlock", "password": "1234"})
            admin.receive_json()
            admin.receive_json()
            visitor.receive_json()
            visitor.send_json({"type": "request"})
            visitor.receive_json()
            admin.receive_json()
            if active_call:
                admin.send_json({"type": "allow", "target": visitor_id})
                visitor.receive_json()
                admin.receive_json()
            admin.send_json({"type": "revoke_invite", "id": invitation["id"]})
            if active_call:
                assert admin.receive_json()["type"] == "call_ended"
            assert visitor.receive_json()["status"] == "revoked"
            assert visitor.receive_json()["peer"] is None
            state = admin.receive_json()
            assert state["requests"] == [] and state["peer"] is None
            assert state["invitations"][0]["status"] == "revoked"
            assert call_room.pending == {} and call_room.approved == {}


def test_expiry_ends_an_approved_call_without_any_client_message(client):
    invitation = create_invitation("Expiring", 60)
    visitor_auth = protocols(client, "Expiring visitor")
    admin_auth = protocols(client, "Expiry admin")
    with client.websocket_connect("/ws/call/room", subprotocols=visitor_auth) as visitor:
        visitor_id = visitor.receive_json()["id"]
        visitor.receive_json()
        join(visitor, invitation)
        with client.websocket_connect("/ws/call/room?role=admin", subprotocols=admin_auth) as admin:
            admin.receive_json()
            admin.receive_json()
            visitor.receive_json()
            admin.send_json({"type": "unlock", "password": "1234"})
            admin.receive_json()
            admin.receive_json()
            visitor.receive_json()
            visitor.send_json({"type": "request"})
            visitor.receive_json()
            admin.receive_json()
            admin.send_json({"type": "allow", "target": visitor_id})
            visitor.receive_json()
            admin.receive_json()
            with SessionLocal() as database:
                row = database.get(VideoChatInvitation, invitation["id"])
                row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                database.commit()
            # The server's monitor must end the call even if both clients are idle.
            assert admin.receive_json()["type"] == "call_ended"
            assert visitor.receive_json()["status"] == "expired"
            assert visitor.receive_json()["peer"] is None
            assert admin.receive_json()["invitations"][0]["status"] == "expired"
            assert call_room.approved == {}
