import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session

router = APIRouter(tags=["health"])
logger = logging.getLogger(__name__)


async def check_database(session: AsyncSession) -> None:
    try:
        await session.execute(text("select 1"))
    except Exception as exc:
        logger.exception("Database health check failed")
        root_cause = getattr(exc, "orig", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "status": "unavailable",
                "dependency": "database",
                "error_type": type(root_cause).__name__,
            },
        ) from exc


@router.get("/health")
async def health_check(session: AsyncSession = Depends(get_session)) -> dict[str, str]:
    await check_database(session)
    return {"status": "ok"}


@router.get("/health/live")
async def liveness_check() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness_check(session: AsyncSession = Depends(get_session)) -> dict[str, str]:
    await check_database(session)
    return {"status": "ok"}
