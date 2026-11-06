from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.websockets import WebSocketDisconnect

from app.routers.video_chat import call_room
from app.services.video_invitations import create_invitation


@pytest.fixture()
def call_invite(client):
    invitation = create_invitation("Test call", 60)
    return parse_qs(urlsplit(invitation["join_path"]).query)["invite"][0]


def join_invitation(websocket, token):
    websocket.send_json({"type": "join_invite", "token": token})
    assert websocket.receive_json()["status"] == "active"
    assert websocket.receive_json()["invite_status"] == "active"


def protocols(client, name):
    response = client.post(
        "/api/auth/guest",
        json={"nickname": name, "sex": "prefer_not_to_say", "preferred_language": "en"},
    )
    assert response.status_code == 201
    return ["lamya-bearer", response.json()["access_token"]]


def test_call_requires_password_and_explicit_admin_approval(client, call_invite):
    visitor_auth = protocols(client, "Call visitor")
    admin_auth = protocols(client, "Call admin")
    with client.websocket_connect("/ws/call/room", subprotocols=visitor_auth) as visitor:
        visitor_id = visitor.receive_json()["id"]
        assert visitor.receive_json()["status"] == "idle"
        join_invitation(visitor, call_invite)
        with client.websocket_connect("/ws/call/room?role=admin", subprotocols=admin_auth) as admin:
            admin_id = admin.receive_json()["id"]
            assert admin.receive_json()["status"] == "locked"
            visitor.receive_json()
            visitor.send_json({"type": "request"})
            waiting = visitor.receive_json()
            assert waiting["status"] == "waiting" and waiting["peer"] is None
            assert admin.receive_json()["requests"] == []
            admin.send_json({"type": "allow", "target": visitor_id})
            assert admin.receive_json()["detail"] == "Unlock call controls first"
            assert call_room.approved == {}
            admin.send_json({"type": "unlock", "password": "wrong"})
            assert admin.receive_json()["granted"] is False
            assert admin.receive_json()["status"] == "locked"
            visitor.receive_json()
            admin.send_json({"type": "unlock", "password": "1234"})
            assert admin.receive_json()["granted"] is True
            state = admin.receive_json()
            assert state["requests"] == [{"id": visitor_id, "name": "Call visitor"}]
            visitor.receive_json()

            # Even after the admin unlocks, offers are blocked until Allow.
            offer = {"kind": "offer", "description": {"type": "offer", "sdp": "test"}}
            admin.send_json({"type": "signal", "target": visitor_id, "signal": offer})
            admin.send_json({"type": "allow", "target": visitor_id})
            assert admin.receive_json()["peer"] == {"id": visitor_id, "name": "Call visitor"}
            approved = visitor.receive_json()
            assert approved["type"] == "state"  # No early offer reached the visitor.
            assert approved["peer"] == {"id": admin_id, "name": "Call admin"}
            assert call_room.approved == {visitor_id: admin_id}

            admin.send_json({"type": "signal", "target": visitor_id, "signal": offer})
            assert visitor.receive_json() == {"type": "signal", "from": admin_id, "signal": offer}
            answer = {"kind": "answer", "description": {"type": "answer", "sdp": "reply"}}
            visitor.send_json({"type": "signal", "target": admin_id, "signal": answer})
            assert admin.receive_json()["signal"] == answer
            ice = {"kind": "ice", "candidate": {"candidate": "test-candidate"}}
            visitor.send_json({"type": "signal", "target": admin_id, "signal": ice})
            assert admin.receive_json()["signal"] == ice
            admin.send_json({"type": "signal", "target": visitor_id, "signal": ice})
            assert visitor.receive_json()["signal"] == ice
            visitor.send_json({"type": "end"})
            assert admin.receive_json()["type"] == "call_ended"
            assert admin.receive_json()["peer"] is None
            assert visitor.receive_json()["status"] == "idle"
            assert call_room.approved == {}
    assert call_room.clients == {}
    assert call_room.people == {}
    assert call_room.pending == {}


def test_requests_can_be_cancelled_declined_and_requested_again(client, call_invite):
    visitor_auth = protocols(client, "Waiting caller")
    admin_auth = protocols(client, "Reviewing admin")
    with client.websocket_connect("/ws/call/room", subprotocols=visitor_auth) as visitor:
        visitor_id = visitor.receive_json()["id"]
        visitor.receive_json()
        join_invitation(visitor, call_invite)
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
            visitor.send_json({"type": "cancel"})
            assert visitor.receive_json()["status"] == "idle"
            assert admin.receive_json()["requests"] == []
            visitor.send_json({"type": "request"})
            visitor.receive_json()
            admin.receive_json()
            admin.send_json({"type": "decline", "target": visitor_id})
            assert visitor.receive_json()["type"] == "call_declined"
            assert visitor.receive_json()["status"] == "idle"
            assert admin.receive_json()["requests"] == []
            visitor.send_json({"type": "request"})
            assert visitor.receive_json()["status"] == "waiting"
            assert admin.receive_json()["requests"][0]["id"] == visitor_id


def test_call_signals_are_pair_scoped_and_admin_cannot_take_a_second_call(client, call_invite):
    first_auth = protocols(client, "Approved caller")
    second_auth = protocols(client, "Other caller")
    admin_auth = protocols(client, "Busy admin")
    with client.websocket_connect("/ws/call/room", subprotocols=first_auth) as first:
        first_id = first.receive_json()["id"]
        first.receive_json()
        join_invitation(first, call_invite)
        with client.websocket_connect("/ws/call/room", subprotocols=second_auth) as second:
            second_id = second.receive_json()["id"]
            second.receive_json()
            first.receive_json()
            join_invitation(second, call_invite)
            first.receive_json()
            with client.websocket_connect(
                "/ws/call/room?role=admin", subprotocols=admin_auth
            ) as admin:
                admin_id = admin.receive_json()["id"]
                admin.receive_json()
                first.receive_json()
                second.receive_json()
                admin.send_json({"type": "unlock", "password": "1234"})
                admin.receive_json()
                admin.receive_json()
                first.receive_json()
                second.receive_json()
                first.send_json({"type": "request"})
                first.receive_json()
                second.receive_json()
                admin.receive_json()
                second.send_json({"type": "request"})
                first.receive_json()
                second.receive_json()
                admin.receive_json()
                admin.send_json({"type": "allow", "target": first_id})
                first.receive_json()
                second.receive_json()
                admin.receive_json()
                admin.send_json({"type": "allow", "target": second_id})
                assert "End your current call" in admin.receive_json()["detail"]
                assert call_room.approved == {first_id: admin_id}
                ice = {"kind": "ice", "candidate": {"candidate": "not-approved"}}
                second.send_json({"type": "signal", "target": admin_id, "signal": ice})
                second.send_json({"type": "signal", "target": first_id, "signal": ice})
                first.send_json({"type": "signal", "target": second_id, "signal": ice})
                admin.send_json({"type": "signal", "target": second_id, "signal": ice})
                # A fresh request-state broadcast is the next message everywhere.
                second.send_json({"type": "request"})
                assert first.receive_json()["type"] == "state"
                assert second.receive_json()["peer"] is None
                assert admin.receive_json()["type"] == "state"
            assert first.receive_json()["type"] == "call_ended"
            assert first.receive_json()["peer"] is None
            second.receive_json()
            assert call_room.approved == {}


def test_visitor_cannot_approve_and_admin_cannot_call_without_a_request(client, call_invite):
    visitor_auth = protocols(client, "Consent caller")
    admin_auth = protocols(client, "Consent admin")
    with client.websocket_connect("/ws/call/room", subprotocols=visitor_auth) as visitor:
        visitor_id = visitor.receive_json()["id"]
        visitor.receive_json()
        join_invitation(visitor, call_invite)
        with client.websocket_connect("/ws/call/room?role=admin", subprotocols=admin_auth) as admin:
            admin_id = admin.receive_json()["id"]
            admin.receive_json()
            visitor.receive_json()
            admin.send_json({"type": "unlock", "password": "1234"})
            admin.receive_json()
            admin.receive_json()
            visitor.receive_json()
            admin.send_json({"type": "allow", "target": visitor_id})
            assert "no longer available" in admin.receive_json()["detail"]
            visitor.send_json({"type": "unlock", "password": "1234"})
            visitor.send_json({"type": "allow", "target": admin_id})
            visitor.send_json({"type": "request"})
            assert visitor.receive_json()["peer"] is None
            assert admin.receive_json()["requests"][0]["id"] == visitor_id
            assert call_room.approved == {}


def test_call_endpoint_rejects_missing_credentials_and_untrusted_origin(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/call/room"):
            pass
    auth = protocols(client, "Origin caller")
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/ws/call/room", subprotocols=auth, headers={"origin": "https://untrusted.test"}
        ):
            pass
