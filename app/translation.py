from collections.abc import Mapping

import httpx
from sqlalchemy.orm import Session

from .config import get_settings
from .models import TranslationSettings
from .services.secrets import SecretResolver

DEFAULT_SYSTEM_PROMPT = "Translate the user's message accurately. Preserve meaning, tone, names, formatting, and URLs. Return only the translation."


class TranslationService:
    def __init__(self, database: Session):
        self.database = database

    def translate(self, text: str, source_language: str, target_language: str) -> str:
        if source_language.lower() == target_language.lower():
            return text
        settings = self.database.get(TranslationSettings, 1)
        provider = settings.provider if settings else get_settings().translation_provider
        if provider == "openai_compatible":
            return self._translate_with_llm(text, source_language, target_language, settings)
        return f"[{target_language}] {text}"

    def bot_reply(self, bot_prompt: str, user_message: str, language: str) -> str:
        settings = self.database.get(TranslationSettings, 1)
        defaults = get_settings()
        provider = settings.provider if settings else defaults.translation_provider
        if provider != "openai_compatible":
            return "Thanks for sharing that. Tell me more about it."
        api_url = (settings.api_url if settings else None) or defaults.translation_api_url
        api_key = (
            SecretResolver().resolve(
                settings.api_key if settings else None,
                settings.api_key_ref if settings else None,
            )
            or defaults.translation_api_key
        )
        model = (settings.model if settings else None) or defaults.translation_model
        if not api_url or not api_key or not model:
            raise ValueError("Translation provider is not fully configured")
        response = httpx.post(
            api_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "temperature": 0.7,
                "messages": [
                    {
                        "role": "system",
                        "content": f"{bot_prompt}\nReply naturally in {language}. Keep replies warm and concise. Return only the reply.",
                    },
                    {"role": "user", "content": user_message},
                ],
            },
            timeout=30,
        )
        response.raise_for_status()
        payload: Mapping[str, object] = response.json()
        choices = payload.get("choices")
        message = (
            choices[0].get("message")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict)
            else None
        )
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Bot provider returned empty content")
        return content.strip()

    @staticmethod
    def _translate_with_llm(
        text: str, source_language: str, target_language: str, settings: TranslationSettings | None
    ) -> str:
        defaults = get_settings()
        api_url = (settings.api_url if settings else None) or defaults.translation_api_url
        api_key = (
            SecretResolver().resolve(
                settings.api_key if settings else None,
                settings.api_key_ref if settings else None,
            )
            or defaults.translation_api_key
        )
        model = (settings.model if settings else None) or defaults.translation_model
        system_prompt = (settings.system_prompt if settings else None) or DEFAULT_SYSTEM_PROMPT
        if not api_url or not api_key or not model:
            raise ValueError("Translation provider is not fully configured")
        response = httpx.post(
            api_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": f"Translate from {source_language} to {target_language}.\n\n{text}",
                    },
                ],
            },
            timeout=30,
        )
        response.raise_for_status()
        payload: Mapping[str, object] = response.json()
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ValueError("Translation provider returned no choices")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Translation provider returned empty content")
        return content.strip()
