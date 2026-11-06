from datetime import datetime, timedelta, timezone

import pytest
from starlette.websockets import WebSocketDisconnect

from app.database import SessionLocal
from app.models import User
from app.routers.video import room


def guest(client, name):
    response = client.post(
        "/api/auth/guest",
        json={"nickname": name, "sex": "prefer_not_to_say", "preferred_language": "en"},
    )
    assert response.status_code == 201
    result = response.json()
    return result, ["lamya-bearer", result["access_token"]]


def test_admin_views_cameras_and_shares_screen_without_visitor_discovery(client):
    first, first_protocols = guest(client, "First visitor")
    _, second_protocols = guest(client, "Second visitor")
    _, admin_protocols = guest(client, "Video controller")
    headers = {"Authorization": f"Bearer {first['access_token']}"}
    assert client.get("/api/auth/me", headers=headers).status_code == 200

    with client.websocket_connect("/ws/live/room", subprotocols=first_protocols) as first_ws:
        first_id = first_ws.receive_json()["id"]
        assert first_ws.receive_json()["participants"] == []
        with client.websocket_connect("/ws/live/room", subprotocols=second_protocols) as second_ws:
            second_id = second_ws.receive_json()["id"]
            assert second_ws.receive_json()["participants"] == []
            assert first_ws.receive_json()["participants"] == []
            with client.websocket_connect(
                "/ws/live/room?role=admin", subprotocols=admin_protocols
            ) as admin_ws:
                admin_id = admin_ws.receive_json()["id"]
                rows = admin_ws.receive_json()["participants"]
                assert {row["id"] for row in rows} == {first_id, second_id}
                assert all(row["role"] == "visitor" and not row["camera"] for row in rows)
                assert first_ws.receive_json()["participants"] == []
                assert second_ws.receive_json()["participants"] == []
                first_ws.send_json({"type": "presence", "camera": True})
                assert first_ws.receive_json()["participants"] == []
                assert second_ws.receive_json()["participants"] == []
                rows = admin_ws.receive_json()["participants"]
                assert next(row for row in rows if row["id"] == first_id)["camera"] is True

                offer = {"kind": "offer", "description": {"type": "offer", "sdp": "test"}}
                admin_ws.send_json(
                    {"type": "signal", "target": first_id, "from": "spoofed", "signal": offer}
                )
                controllers = first_ws.receive_json()["participants"]
                assert [row["id"] for row in controllers] == [admin_id]
                assert first_ws.receive_json() == {
                    "type": "signal", "from": admin_id, "signal": offer
                }
                assert second_ws.receive_json()["participants"] == []
                admin_ws.receive_json()
                answer = {"kind": "answer", "description": {"type": "answer", "sdp": "reply"}}
                first_ws.send_json({"type": "signal", "target": admin_id, "signal": answer})
                assert admin_ws.receive_json()["signal"] == answer
                admin_ws.send_json(
                    {"type": "signal", "target": first_id, "signal": {"kind": "screen", "enabled": True}}
                )
                assert first_ws.receive_json()["signal"] == {"kind": "screen", "enabled": True}

                # Visitors cannot initiate offers, discover peers, or send screens.
                first_ws.send_text("invalid json")
                first_ws.send_json(["invalid payload"])
                first_ws.send_json(
                    {"type": "signal", "target": admin_id, "signal": {"kind": {}}}
                )
                first_ws.send_json({"type": "signal", "target": second_id, "signal": offer})
                first_ws.send_json({"type": "signal", "target": admin_id, "signal": offer})
                first_ws.send_json(
                    {"type": "signal", "target": admin_id, "signal": {"kind": "screen", "enabled": True}}
                )
                first_ws.send_json({"type": "presence", "camera": False})
                # The next message must be the roster, not any prohibited signal.
                rows = admin_ws.receive_json()["participants"]
                assert next(row for row in rows if row["id"] == first_id)["camera"] is False
                first_ws.receive_json()
                assert second_ws.receive_json()["participants"] == []
                admin_ws.send_json(
                    {"type": "signal", "target": first_id, "signal": {"kind": "hangup"}}
                )
                assert first_ws.receive_json()["signal"]["kind"] == "hangup"
                assert first_ws.receive_json()["participants"] == []
                assert second_ws.receive_json()["participants"] == []
                admin_ws.receive_json()
            assert first_ws.receive_json()["participants"] == []
            second_ws.receive_json()
        assert first_ws.receive_json()["participants"] == []
    assert room.clients == {}
    assert room.participants == {}
    assert room.links == set()


def test_video_rejects_missing_credentials_and_untrusted_origins(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/live/room"):
            pass
    _, protocols = guest(client, "Origin visitor")
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/ws/live/room", subprotocols=protocols, headers={"origin": "https://untrusted.test"}
        ):
            pass


def test_expired_guest_can_be_replaced_without_login(client):
    result, protocols = guest(client, "Expired visitor")
    with SessionLocal() as database:
        user = database.get(User, result["user"]["id"])
        user.guest_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        database.commit()
    headers = {"Authorization": f"Bearer {result['access_token']}"}
    assert client.get("/api/auth/me", headers=headers).status_code == 401
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/live/room", subprotocols=protocols):
            pass
    fresh, _ = guest(client, "Fresh visitor")
    assert client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {fresh['access_token']}"}
    ).status_code == 200
