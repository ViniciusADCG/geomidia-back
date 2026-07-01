from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session


router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check(session: AsyncSession = Depends(get_session)) -> dict[str, str]:
    await session.execute(text("select 1"))
    return {"status": "ok"}
