from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ActivityLog
from app.db.session import get_session
from app.schemas import ActivityLogRead


router = APIRouter(prefix="/activities", tags=["activities"])


@router.get("", response_model=list[ActivityLogRead])
async def list_activities(
    limit: int = Query(default=30, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
) -> list[ActivityLog]:
    result = await session.scalars(select(ActivityLog).order_by(desc(ActivityLog.created_at)).limit(limit))
    return list(result)
