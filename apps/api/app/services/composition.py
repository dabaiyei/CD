from __future__ import annotations

from decimal import Decimal

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CompositionStatus, CompositionVersion


async def invalidate_compositions(
    session: AsyncSession,
    *,
    chapter_id: str,
    reason: str,
) -> None:
    await session.execute(
        update(CompositionVersion)
        .where(
            CompositionVersion.chapter_id == chapter_id,
            CompositionVersion.status.in_(
                {
                    CompositionStatus.DRAFT,
                    CompositionStatus.RENDERING,
                    CompositionStatus.READY,
                }
            ),
        )
        .values(
            status=CompositionStatus.STALE,
            is_active=False,
            invalidated_reason=reason,
        )
    )


def estimate_dialogue_duration(text: str) -> Decimal:
    return max(Decimal("1.2"), min(Decimal("12"), Decimal(len(text)) * Decimal("0.22")))
