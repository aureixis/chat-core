from __future__ import annotations

import logging

import httpx
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import AISettings
from .policy import PolicyDecision, evaluate_generated_content, evaluate_user_content
from .secrets import SecretResolver

logger = logging.getLogger(__name__)


class ModerationService:
    def __init__(self, database: Session):
        self.database = database

    def evaluate(
        self, text: str, *, generated: bool, blocked_topics: list[str] | None = None
    ) -> PolicyDecision:
        deterministic = (
            evaluate_generated_content(text, blocked_topics)
            if generated
            else evaluate_user_content(text)
        )
        if not deterministic.allowed:
            return deterministic

        row = self.database.get(AISettings, 1)
        defaults = get_settings()
        provider = row.provider if row else defaults.ai_provider
        moderation_model = row.moderation_model if row else None
        enabled = row.enabled if row else True
        if provider != "openai_compatible" or not enabled or not moderation_model:
            return deterministic
        api_url = (row.api_url if row else None) or defaults.ai_api_url
        try:
            api_key = (
                SecretResolver().resolve(
                    row.api_key if row else None,
                    row.api_key_ref if row else None,
                )
                or defaults.ai_api_key
            )
        except ValueError as error:
            logger.warning("Moderation credential resolution failed: %s", error)
            return self._provider_failure("moderation_provider_incomplete", generated)
        if not api_url or not api_key:
            return self._provider_failure("moderation_provider_incomplete", generated)
        try:
            response = httpx.post(
                api_url.rstrip("/") + "/moderations",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": moderation_model, "input": text},
                timeout=20,
            )
            response.raise_for_status()
            payload = response.json()
            results = payload.get("results") or []
            if not results or not isinstance(results[0], dict):
                raise ValueError("Moderation provider returned no result")
            result = results[0]
            if not result.get("flagged"):
                return deterministic
            categories = result.get("categories") or {}
            reasons = [name for name, flagged in categories.items() if flagged]
            return PolicyDecision(
                False, "blocked" if generated else "flagged", reasons or ["provider_flagged"]
            )
        except Exception as error:
            logger.warning("Moderation provider failed: %s", error)
            return self._provider_failure("moderation_provider_unavailable", generated)

    @staticmethod
    def _provider_failure(reason: str, generated: bool) -> PolicyDecision:
        if not get_settings().moderation_fail_closed:
            return PolicyDecision(True, "approved", [])
        return PolicyDecision(False, "blocked" if generated else "flagged", [reason])
