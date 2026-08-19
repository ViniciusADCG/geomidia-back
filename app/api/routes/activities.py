from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import require_roles
from app.db.models import ActivityLog, User
from app.db.session import get_session
from app.schemas import ActivityLogPage

router = APIRouter(prefix="/activities", tags=["activities"])


@router.get("", response_model=ActivityLogPage)
async def list_activities(
    limit: int = Query(default=30, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_roles("admin", "analyst", "viewer")),
) -> ActivityLogPage:
    total = await session.scalar(select(func.count()).select_from(ActivityLog)) or 0
    result = await session.scalars(
        select(ActivityLog).order_by(desc(ActivityLog.created_at)).offset(offset).limit(limit)
    )
    return ActivityLogPage(items=list(result), total=total, limit=limit, offset=offset)
