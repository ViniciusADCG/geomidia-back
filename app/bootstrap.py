from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.security import hash_password
from app.db.models import User


async def ensure_admin(session: AsyncSession, settings: Settings) -> bool:
    if not settings.bootstrap_admin_username or not settings.bootstrap_admin_password:
        raise RuntimeError("Defina BOOTSTRAP_ADMIN_USERNAME e BOOTSTRAP_ADMIN_PASSWORD antes do provisionamento.")

    username = settings.bootstrap_admin_username.strip().lower()
    existing = await session.scalar(select(User).where(User.username == username))
    if existing is not None:
        return False

    session.add(
        User(
            username=username,
            full_name=settings.bootstrap_admin_name,
            password_hash=hash_password(settings.bootstrap_admin_password),
            role="admin",
            is_active=True,
        )
    )
    await session.commit()
    return True
