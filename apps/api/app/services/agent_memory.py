from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AgentMemory, User

EMBEDDING_DIMENSIONS = 384
EMBEDDING_MODEL = "cineforge-hash-v1"
MAX_RETRIEVAL_CANDIDATES = 500


@dataclass(frozen=True)
class RetrievedMemory:
    memory: AgentMemory
    score: float

    def as_context(self) -> str:
        return (
            f"[{self.memory.namespace}/{self.memory.memory_key}; "
            f"relevance={self.score:.3f}] {self.memory.content}"
        )


def embed_memory_text(content: str) -> list[float]:
    normalized = unicodedata.normalize("NFKC", content).casefold()
    words = re.findall(r"[a-z0-9]+|[\u3400-\u9fff]", normalized)
    features = list(words)
    features.extend(f"{left}:{right}" for left, right in zip(words, words[1:], strict=False))
    compact = "".join(words)
    features.extend(compact[index : index + 3] for index in range(max(0, len(compact) - 2)))
    if not features:
        return [0.0] * EMBEDDING_DIMENSIONS

    vector = [0.0] * EMBEDDING_DIMENSIONS
    for feature in features[:20_000]:
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        bucket = int.from_bytes(digest[:4], "big") % EMBEDDING_DIMENSIONS
        sign = 1.0 if digest[4] & 1 else -1.0
        vector[bucket] += sign
    magnitude = math.sqrt(sum(value * value for value in vector))
    if magnitude == 0:
        return vector
    return [value / magnitude for value in vector]


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    return max(-1.0, min(1.0, sum(a * b for a, b in zip(left, right, strict=True))))


def _recency_score(updated_at: datetime, now: datetime) -> float:
    timestamp = updated_at if updated_at.tzinfo else updated_at.replace(tzinfo=UTC)
    age_days = max(0.0, (now - timestamp).total_seconds() / 86_400)
    return math.exp(-age_days / 120)


async def retrieve_project_memories(
    session: AsyncSession,
    *,
    user: User,
    project_id: str | None,
    query: str,
    limit: int = 8,
) -> list[RetrievedMemory]:
    if limit <= 0:
        return []
    scope = (
        AgentMemory.tenant_id == user.tenant_id,
        AgentMemory.user_id == user.id,
        AgentMemory.project_id == project_id,
    )
    query_vector = embed_memory_text(query)
    dialect = session.bind.dialect.name if session.bind is not None else ""
    if query.strip() and dialect == "postgresql":
        statement = (
            select(AgentMemory)
            .where(*scope, AgentMemory.embedding.is_not(None))
            .order_by(AgentMemory.embedding.cosine_distance(query_vector))
            .limit(min(MAX_RETRIEVAL_CANDIDATES, max(limit * 8, 32)))
        )
    else:
        statement = (
            select(AgentMemory)
            .where(*scope)
            .order_by(AgentMemory.updated_at.desc())
            .limit(MAX_RETRIEVAL_CANDIDATES)
        )
    rows = list((await session.scalars(statement)).all())
    if not rows:
        return []

    now = datetime.now(UTC)
    ranked: list[RetrievedMemory] = []
    for memory in rows:
        vector = list(memory.embedding or embed_memory_text(memory.content))
        similarity = max(0.0, cosine_similarity(query_vector, vector)) if query.strip() else 0.0
        score = similarity * 0.76 + float(memory.salience or 0.5) * 0.16
        score += _recency_score(memory.updated_at, now) * 0.08
        ranked.append(RetrievedMemory(memory=memory, score=score))
    ranked.sort(key=lambda item: (item.score, item.memory.updated_at), reverse=True)
    return ranked[:limit]


def normalize_memory_namespace(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9._-]+", "-", value.casefold()).strip("-.")
    return (normalized or "project")[:80]


def normalize_memory_key(value: str, content: str) -> str:
    normalized = re.sub(r"[^a-z0-9._-]+", "-", value.casefold()).strip("-.")
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    return (normalized[:140].rstrip("-.") or f"memory-{digest}")[:160]
