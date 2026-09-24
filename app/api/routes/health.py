import logging
import os
from collections.abc import Mapping

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import get_session

router = APIRouter(tags=["health"])
logger = logging.getLogger(__name__)


def preview_database_diagnostics(error: Exception, database_url: str, environment: Mapping[str, str]) -> dict[str, object]:
    url = make_url(database_url)
    message = str(error).lower()
    if "authentication failed" in message:
        category = "authentication"
    elif any(phrase in message for phrase in ("could not translate host name", "name or service not known", "name resolution")):
        category = "dns"
    elif "connection refused" in message:
        category = "connection_refused"
    elif "timed out" in message or "timeout" in message:
        category = "timeout"
    elif "certificate" in message or "ssl" in message or "tls" in message:
        category = "tls"
    elif "no route to host" in message or "network is unreachable" in message:
        category = "network"
    else:
        category = "other"

    return {
        "database_url_configured": bool(environment.get("DATABASE_URL")),
        "database_target": "local" if url.host in {"localhost", "127.0.0.1", "::1"} else "remote",
        "database_port": url.port,
        "connection_issue": category,
    }


async def check_database(session: AsyncSession) -> None:
    try:
        await session.execute(text("select 1"))
    except Exception as exc:
        logger.exception("Database health check failed")
        root_cause = getattr(exc, "orig", exc)
        detail: dict[str, object] = {
            "status": "unavailable",
            "dependency": "database",
            "error_type": type(root_cause).__name__,
        }
        if os.environ.get("VERCEL_ENV") == "preview":
            detail.update(preview_database_diagnostics(root_cause, get_settings().database_url, os.environ))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=detail,
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
