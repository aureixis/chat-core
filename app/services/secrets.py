from __future__ import annotations

import base64
import hashlib
import os
import re
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from ..config import get_settings

_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")
_ENCRYPTED_PREFIX = "enc:v1:"


class SecretResolver:
    def __init__(self):
        settings = get_settings()
        self.environment = settings.environment
        self.file_root = Path(settings.secret_file_root).resolve()
        self._fernet = None
        if settings.secrets_encryption_key:
            if len(settings.secrets_encryption_key) < 32:
                raise ValueError("SECRETS_ENCRYPTION_KEY must contain at least 32 characters")
            derived = hashlib.sha256(settings.secrets_encryption_key.encode("utf-8")).digest()
            self._fernet = Fernet(base64.urlsafe_b64encode(derived))

    def resolve(self, stored_value: str | None, reference: str | None) -> str | None:
        if reference:
            return self._resolve_reference(reference)
        if not stored_value:
            return None
        if not stored_value.startswith(_ENCRYPTED_PREFIX):
            return stored_value
        if not self._fernet:
            raise ValueError("SECRETS_ENCRYPTION_KEY is required to decrypt provider credentials")
        try:
            token = stored_value.removeprefix(_ENCRYPTED_PREFIX).encode("ascii")
            return self._fernet.decrypt(token).decode("utf-8")
        except (InvalidToken, UnicodeError, ValueError) as error:
            raise ValueError("Stored provider credential could not be decrypted") from error

    def protect(self, value: str) -> str:
        if not value:
            raise ValueError("Secret value cannot be empty")
        if not self._fernet:
            if self.environment.lower() == "production":
                raise ValueError(
                    "Use a secret reference or configure SECRETS_ENCRYPTION_KEY in production"
                )
            return value
        token = self._fernet.encrypt(value.encode("utf-8")).decode("ascii")
        return _ENCRYPTED_PREFIX + token

    def _resolve_reference(self, reference: str) -> str:
        if reference.startswith("env://"):
            name = reference.removeprefix("env://")
            if not _ENV_NAME.fullmatch(name):
                raise ValueError("Invalid environment secret reference")
            value = os.getenv(name)
            if not value:
                raise ValueError(f"Secret environment variable {name} is unavailable")
            return value
        if reference.startswith("file://"):
            path = Path(reference.removeprefix("file://")).resolve()
            if not path.is_relative_to(self.file_root):
                raise ValueError("Secret file must be located below SECRET_FILE_ROOT")
            if not path.is_file():
                raise ValueError("Secret file is unavailable")
            return path.read_text(encoding="utf-8").strip()
        raise ValueError("Secret references must use env:// or file://")


def secret_is_configured(stored_value: str | None, reference: str | None) -> bool:
    return bool(stored_value or reference)
