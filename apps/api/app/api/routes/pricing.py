from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.models import PricingRule, User
from app.db.session import get_session
from app.domain.schemas import PricingRulePublic

router = APIRouter(prefix="/pricing", tags=["pricing"])


@router.get("", response_model=list[PricingRulePublic])
async def list_current_pricing(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[PricingRule]:
    return list(
        (
            await session.scalars(
                select(PricingRule)
                .where(PricingRule.tenant_id == user.tenant_id)
                .order_by(PricingRule.display_order, PricingRule.name)
            )
        ).all()
    )
