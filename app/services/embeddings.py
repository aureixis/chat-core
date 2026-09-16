from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass

import httpx
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import AISettings
from ..vector_config import (
    KNOWLEDGE_CHUNK_OVERLAP,
    KNOWLEDGE_CHUNK_SIZE,
    MAX_KNOWLEDGE_CHUNKS,
    VECTOR_DIMENSIONS,
)
from .secrets import SecretResolver

_TOKEN_PATTERN = re.compile(r"[\w'-]+", re.UNICODE)


class EmbeddingsDisabledError(ValueError):
    pass


@dataclass
class EmbeddingResult:
    vectors: list[list[float]]
    provider: str
    model: str
    input_tokens: int = 0


def content_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def chunk_text(
    value: str,
    *,
    size: int = KNOWLEDGE_CHUNK_SIZE,
    overlap: int = KNOWLEDGE_CHUNK_OVERLAP,
    limit: int = MAX_KNOWLEDGE_CHUNKS,
) -> list[str]:
    text = re.sub(r"[ \t]+", " ", value).strip()
    if not text:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(text) and len(chunks) < limit:
        proposed_end = min(start + size, len(text))
        end = proposed_end
        if proposed_end < len(text):
            search_from = start + max(size // 2, 1)
            candidates = [
                text.rfind("\n", search_from, proposed_end),
                text.rfind(". ", search_from, proposed_end),
                text.rfind(" ", search_from, proposed_end),
            ]
            boundary = max(candidates)
            if boundary > start:
                end = boundary + 1
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


class EmbeddingService:
    def __init__(self, database: Session):
        row = database.get(AISettings, 1)
        defaults = get_settings()
        self.provider = row.provider if row else defaults.ai_provider
        self.api_url = (row.api_url if row else None) or defaults.ai_api_url
        self.api_key = (
            SecretResolver().resolve(row.api_key if row else None, row.api_key_ref if row else None)
            or defaults.ai_api_key
        )
        self.model = (row.embedding_model if row else None) or defaults.embedding_model
        self.dimensions = row.embedding_dimensions if row else defaults.embedding_dimensions
        self.enabled = row.embeddings_enabled if row else defaults.embeddings_enabled
        if self.dimensions != VECTOR_DIMENSIONS:
            raise ValueError(
                f"Embedding dimension is {self.dimensions}; the database requires {VECTOR_DIMENSIONS}"
            )

    def embed_texts(self, texts: list[str]) -> EmbeddingResult:
        if not self.enabled:
            raise EmbeddingsDisabledError("Vector embeddings are disabled")
        cleaned = [text.strip() for text in texts]
        if not cleaned or any(not text for text in cleaned):
            raise ValueError("Embedding input must contain non-empty text")
        if self.provider == "local":
            return EmbeddingResult(
                vectors=[self._local_vector(text) for text in cleaned],
                provider="local",
                model="local-feature-hash-v1",
                input_tokens=sum(max(1, math.ceil(len(text) / 4)) for text in cleaned),
            )
        if self.provider != "openai_compatible":
            raise ValueError(f"Unsupported embedding provider: {self.provider}")
        return self._remote_embeddings(cleaned)

    def embed_query(self, text: str) -> list[float]:
        return self.embed_texts([text]).vectors[0]

    def _local_vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        tokens = [token.casefold() for token in _TOKEN_PATTERN.findall(text)]
        features: list[tuple[str, float]] = [(f"word:{token}", 2.0) for token in tokens]
        for token in tokens:
            padded = f"^{token}$"
            features.extend(
                (f"char:{padded[index : index + 3]}", 0.35)
                for index in range(max(len(padded) - 2, 1))
            )
        if not features:
            features = [(f"text:{text.casefold()}", 1.0)]
        for feature, weight in features:
            digest = hashlib.sha256(feature.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[index] += sign * weight
        magnitude = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / magnitude for value in vector]

    def _remote_embeddings(self, texts: list[str]) -> EmbeddingResult:
        if not self.api_url or not self.api_key or not self.model:
            raise ValueError("Embedding provider is incomplete")
        response = httpx.post(
            self.api_url.rstrip("/") + "/embeddings",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": self.model,
                "input": texts,
                "dimensions": self.dimensions,
                "encoding_format": "float",
            },
            timeout=45,
        )
        response.raise_for_status()
        payload = response.json()
        data = sorted(payload.get("data") or [], key=lambda item: int(item.get("index", 0)))
        if len(data) != len(texts):
            raise ValueError("Embedding provider returned an unexpected number of vectors")
        if [int(item.get("index", -1)) for item in data] != list(range(len(texts))):
            raise ValueError("Embedding provider returned invalid vector indexes")
        vectors = [list(map(float, item.get("embedding") or [])) for item in data]
        if any(len(vector) != self.dimensions for vector in vectors):
            raise ValueError(f"Embedding provider must return exactly {self.dimensions} dimensions")
        if any(
            not all(math.isfinite(value) for value in vector)
            or not math.sqrt(sum(value * value for value in vector))
            for vector in vectors
        ):
            raise ValueError("Embedding provider returned an invalid zero or non-finite vector")
        usage = payload.get("usage") or {}
        estimated_tokens = sum(max(1, math.ceil(len(text) / 4)) for text in texts)
        return EmbeddingResult(
            vectors=vectors,
            provider=self.provider,
            model=self.model,
            input_tokens=int(
                usage.get("prompt_tokens") or usage.get("total_tokens") or estimated_tokens
            ),
        )
