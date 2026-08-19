from collections import defaultdict, deque
from time import monotonic

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, get_current_user, verify_password
from app.db.models import User
from app.db.session import get_session
from app.schemas import LoginRequest, LoginResponse, UserRead

router = APIRouter(prefix="/auth", tags=["auth"])
LOGIN_WINDOW_SECONDS = 5 * 60
LOGIN_MAX_ATTEMPTS = 5
login_attempts: dict[str, deque[float]] = defaultdict(deque)


def enforce_login_rate_limit(client_key: str) -> None:
    now = monotonic()
    attempts = login_attempts[client_key]
    while attempts and attempts[0] < now - LOGIN_WINDOW_SECONDS:
        attempts.popleft()
    if len(attempts) >= LOGIN_MAX_ATTEMPTS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Muitas tentativas. Aguarde alguns minutos antes de tentar novamente.",
        )
    attempts.append(now)


@router.post("/login", response_model=LoginResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> LoginResponse:
    client_host = request.client.host if request.client else "unknown"
    client_key = f"{client_host}:{payload.username}"
    enforce_login_rate_limit(client_key)

    user = await session.scalar(select(User).where(User.username == payload.username))
    if user is None or not user.is_active or not verify_password(payload.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Usuario ou senha invalidos.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    login_attempts.pop(client_key, None)
    token, expires_at = create_access_token(user)
    return LoginResponse(
        access_token=token,
        user_name=user.full_name,
        user_id=user.id,
        role=user.role,
        expires_at=expires_at,
    )


@router.get("/me", response_model=UserRead)
async def read_current_user(current_user: User = Depends(get_current_user)) -> User:
    return current_user
