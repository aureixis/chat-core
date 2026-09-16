import pytest

from app.config import Settings, get_settings
from app.services.secrets import SecretResolver


def test_secret_resolver_encrypts_and_resolves_references(monkeypatch, tmp_path):
    settings = get_settings()
    monkeypatch.setattr(settings, "secrets_encryption_key", "s" * 32)
    monkeypatch.setattr(settings, "secret_file_root", str(tmp_path))
    resolver = SecretResolver()

    protected = resolver.protect("provider-secret")
    assert protected.startswith("enc:v1:")
    assert "provider-secret" not in protected
    assert resolver.resolve(protected, None) == "provider-secret"

    monkeypatch.setenv("LAMYA_TEST_SECRET", "environment-secret")
    assert resolver.resolve(None, "env://LAMYA_TEST_SECRET") == "environment-secret"
    secret_file = tmp_path / "provider-key"
    secret_file.write_text("file-secret\n", encoding="utf-8")
    assert resolver.resolve(None, f"file://{secret_file}") == "file-secret"
    with pytest.raises(ValueError, match="below SECRET_FILE_ROOT"):
        resolver.resolve(None, f"file://{tmp_path.parent / 'outside-secret'}")


def test_production_configuration_rejects_insecure_provider_transport():
    settings = Settings(
        environment="production",
        moderation_fail_closed=True,
        metrics_token="m" * 24,
        auto_create_schema=False,
        ai_api_url="http://model.internal/v1",
    )
    with pytest.raises(ValueError, match="AI_API_URL must use HTTPS"):
        settings.validate_vector_configuration()


def test_audit_outbox_exports_hash_chained_batch(client, monkeypatch):
    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models import AuditOutbox
    from app.services.audit import record_audit
    from app.services.audit_export import export_pending_audit_events

    settings = get_settings()
    monkeypatch.setattr(settings, "audit_export_url", "https://siem.example.test/events")

    class Response:
        def raise_for_status(self):
            return None

    captured = {}

    def post(_client, url, *, headers, json):
        captured.update(url=url, headers=headers, body=json)
        return Response()

    monkeypatch.setattr("httpx.Client.post", post)
    with SessionLocal() as database:
        event = record_audit(database, "test.export", "test_entity", "one")
        database.commit()
        assert export_pending_audit_events(database) == 1
        database.commit()
        outbox = database.scalar(select(AuditOutbox).where(AuditOutbox.event_id == event.id))
        assert outbox.status == "exported"
    assert captured["url"] == "https://siem.example.test/events"
    assert captured["body"]["events"][0]["event_hash"]
