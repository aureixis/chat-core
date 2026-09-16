from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..models import (
    BotAction,
    BotProfile,
    Companion,
    KnowledgeChunk,
    RobotKnowledgeSource,
    RobotMemory,
    UsageLedger,
)
from .embeddings import EmbeddingService, content_hash

logger = logging.getLogger(__name__)
_TERM_PATTERN = re.compile(r"[\w'-]{2,}", re.UNICODE)


@dataclass(frozen=True)
class SearchHit:
    entity_id: int
    content: str
    score: float
    source_id: int | None = None


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    numerator = sum(a * b for a, b in zip(left, right, strict=False))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return numerator / (left_norm * right_norm)


def _terms(value: str) -> set[str]:
    return {term.casefold() for term in _TERM_PATTERN.findall(value)}


def _lexical_score(query_terms: set[str], content: str) -> float:
    if not query_terms:
        return 0.0
    lowered = content.casefold()
    return sum(term in lowered for term in query_terms) / len(query_terms)


class VectorSearchService:
    def __init__(self, database: Session, action_id: int | None = None):
        self.database = database
        self.action_id = action_id

    def search_memories(
        self, bot_id: int, user_id: int, query: str, *, limit: int = 8
    ) -> list[SearchHit]:
        consent = self.database.scalar(
            select(Companion.id).where(
                Companion.bot_id == bot_id,
                Companion.user_id == user_id,
                Companion.status == "active",
                Companion.memory_enabled.is_(True),
            )
        )
        if consent is None:
            return []

        now = datetime.now(timezone.utc)
        criteria = (
            RobotMemory.bot_id == bot_id,
            RobotMemory.user_id == user_id,
            RobotMemory.active.is_(True),
            or_(RobotMemory.expires_at.is_(None), RobotMemory.expires_at > now),
        )
        query_terms = _terms(query)
        candidate_limit = 500 if not self._is_postgresql else max(limit * 4, 30)
        rows_by_id = {
            row.id: row
            for row in self.database.scalars(
                select(RobotMemory)
                .where(*criteria)
                .order_by(RobotMemory.importance.desc(), RobotMemory.created_at.desc())
                .limit(candidate_limit)
            )
        }
        if self._is_postgresql and query_terms:
            lexical_filter = or_(
                *[
                    func.lower(RobotMemory.content).contains(term, autoescape=True)
                    for term in sorted(query_terms)[:12]
                ]
            )
            for row in self.database.scalars(
                select(RobotMemory)
                .where(*criteria, lexical_filter)
                .order_by(RobotMemory.importance.desc(), RobotMemory.created_at.desc())
                .limit(max(limit * 8, 50))
            ):
                rows_by_id[row.id] = row
        rows = list(rows_by_id.values())
        if not rows:
            return []

        vector_scores = self._memory_vector_scores(bot_id, criteria, rows, query, limit)
        missing_ids = set(vector_scores) - set(rows_by_id)
        if missing_ids:
            for row in self.database.scalars(
                select(RobotMemory).where(*criteria, RobotMemory.id.in_(missing_ids))
            ):
                rows_by_id[row.id] = row
            rows = list(rows_by_id.values())
        hits = []
        for rank, row in enumerate(rows):
            semantic = vector_scores.get(row.id, 0.0)
            lexical = _lexical_score(query_terms, row.content)
            importance = max(0.0, min(float(row.importance), 1.0))
            recency = 1.0 / (rank + 1)
            score = semantic * 0.65 + lexical * 0.2 + importance * 0.1 + recency * 0.05
            hits.append(SearchHit(entity_id=row.id, content=row.content, score=score))
        selected = sorted(hits, key=lambda hit: hit.score, reverse=True)[:limit]
        self._record_retrieval("memory", selected)
        return selected

    def search_knowledge(self, bot_id: int, query: str, *, limit: int = 8) -> list[SearchHit]:
        criteria = (
            KnowledgeChunk.bot_id == bot_id,
            KnowledgeChunk.embedding_status == "ready",
            RobotKnowledgeSource.status == "ready",
        )
        candidate_limit = 500 if not self._is_postgresql else max(limit * 4, 30)
        rows_by_id = {
            row.id: row
            for row in self.database.scalars(
                select(KnowledgeChunk)
                .join(RobotKnowledgeSource, RobotKnowledgeSource.id == KnowledgeChunk.source_id)
                .where(*criteria)
                .order_by(KnowledgeChunk.updated_at.desc())
                .limit(candidate_limit)
            )
        }
        query_terms = _terms(query)
        if self._is_postgresql and query_terms:
            lexical_filter = or_(
                *[
                    func.lower(KnowledgeChunk.content).contains(term, autoescape=True)
                    for term in sorted(query_terms)[:12]
                ]
            )
            for row in self.database.scalars(
                select(KnowledgeChunk)
                .join(RobotKnowledgeSource, RobotKnowledgeSource.id == KnowledgeChunk.source_id)
                .where(*criteria, lexical_filter)
                .order_by(KnowledgeChunk.updated_at.desc())
                .limit(max(limit * 8, 50))
            ):
                rows_by_id[row.id] = row
        rows = list(rows_by_id.values())
        if not rows:
            return []

        vector_scores = self._knowledge_vector_scores(bot_id, criteria, rows, query, limit)
        missing_ids = set(vector_scores) - set(rows_by_id)
        if missing_ids:
            for row in self.database.scalars(
                select(KnowledgeChunk)
                .join(RobotKnowledgeSource, RobotKnowledgeSource.id == KnowledgeChunk.source_id)
                .where(*criteria, KnowledgeChunk.id.in_(missing_ids))
            ):
                rows_by_id[row.id] = row
            rows = list(rows_by_id.values())
        hits = []
        for rank, row in enumerate(rows):
            semantic = vector_scores.get(row.id, 0.0)
            lexical = _lexical_score(query_terms, row.content)
            recency = 1.0 / (rank + 1)
            score = semantic * 0.75 + lexical * 0.2 + recency * 0.05
            hits.append(
                SearchHit(
                    entity_id=row.id,
                    source_id=row.source_id,
                    content=row.content,
                    score=score,
                )
            )
        selected = sorted(hits, key=lambda hit: hit.score, reverse=True)[:limit]
        self._record_retrieval("knowledge", selected)
        return selected

    def _memory_vector_scores(
        self,
        bot_id: int,
        criteria: tuple,
        rows: list[RobotMemory],
        query: str,
        limit: int,
    ) -> dict[int, float]:
        try:
            query_vector = self._query_vector(bot_id, query)
            if self._is_postgresql:
                distance = RobotMemory.embedding.cosine_distance(query_vector)
                with self.database.begin_nested():
                    candidates = self.database.execute(
                        select(RobotMemory.id, distance.label("distance"))
                        .where(
                            *criteria,
                            RobotMemory.embedding_status == "ready",
                            RobotMemory.embedding.is_not(None),
                        )
                        .order_by(distance)
                        .limit(max(limit * 6, 30))
                    ).all()
                return {
                    row_id: max(0.0, min(1.0, 1.0 - float(distance_value)))
                    for row_id, distance_value in candidates
                }
            return {
                row.id: max(0.0, _cosine_similarity(query_vector, list(row.embedding)))
                for row in rows
                if row.embedding_status == "ready" and row.embedding is not None
            }
        except Exception as error:
            logger.warning("Memory vector search fell back to lexical ranking: %s", error)
            return {}

    def _knowledge_vector_scores(
        self,
        bot_id: int,
        criteria: tuple,
        rows: list[KnowledgeChunk],
        query: str,
        limit: int,
    ) -> dict[int, float]:
        try:
            query_vector = self._query_vector(bot_id, query)
            if self._is_postgresql:
                distance = KnowledgeChunk.embedding.cosine_distance(query_vector)
                with self.database.begin_nested():
                    candidates = self.database.execute(
                        select(KnowledgeChunk.id, distance.label("distance"))
                        .join(
                            RobotKnowledgeSource,
                            RobotKnowledgeSource.id == KnowledgeChunk.source_id,
                        )
                        .where(*criteria, KnowledgeChunk.embedding.is_not(None))
                        .order_by(distance)
                        .limit(max(limit * 6, 30))
                    ).all()
                return {
                    row_id: max(0.0, min(1.0, 1.0 - float(distance_value)))
                    for row_id, distance_value in candidates
                }
            return {
                row.id: max(0.0, _cosine_similarity(query_vector, list(row.embedding)))
                for row in rows
                if row.embedding is not None
            }
        except Exception as error:
            logger.warning("Knowledge vector search fell back to lexical ranking: %s", error)
            return {}

    @property
    def _is_postgresql(self) -> bool:
        return bool(self.database.bind and self.database.bind.dialect.name == "postgresql")

    def _query_vector(self, bot_id: int, query: str) -> list[float]:
        result = EmbeddingService(self.database).embed_texts([query])
        self.database.add(
            UsageLedger(
                bot_id=bot_id,
                organization_id=self._organization_id(bot_id),
                action_id=self.action_id,
                provider=result.provider,
                model=result.model,
                input_tokens=result.input_tokens,
                output_tokens=0,
                estimated_cost_usd=0,
            )
        )
        if self.action_id is not None:
            action = self.database.get(BotAction, self.action_id)
            if action:
                action.input_tokens = (action.input_tokens or 0) + result.input_tokens
        return result.vectors[0]

    def _organization_id(self, bot_id: int) -> int | None:
        return self.database.scalar(
            select(BotProfile.organization_id).where(BotProfile.bot_id == bot_id)
        )

    def _record_retrieval(self, kind: str, hits: list[SearchHit]) -> None:
        if self.action_id is None or not hits:
            return
        action = self.database.get(BotAction, self.action_id)
        if not action:
            return
        additions = [
            {
                "kind": kind,
                "entity_id": hit.entity_id,
                "source_id": hit.source_id,
                "score": round(hit.score, 6),
                "content_hash": content_hash(hit.content),
            }
            for hit in hits
        ]
        action.retrieval_context = [*(action.retrieval_context or []), *additions]
