from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password, require_roles
from app.db.models import User
from app.db.session import get_session
from app.schemas import UserCreate, UserRead, UserUpdate

router = APIRouter(prefix="/users", tags=["users"])


@router.get("", response_model=list[UserRead])
async def list_users(
    limit: int = Query(default=100, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_roles("admin")),
) -> list[User]:
    return list(await session.scalars(select(User).order_by(User.full_name).limit(limit)))


@router.post("", response_model=UserRead, status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: UserCreate,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_roles("admin")),
) -> User:
    user = User(
        username=payload.username,
        full_name=payload.full_name.strip(),
        email=str(payload.email).lower() if payload.email else None,
        password_hash=hash_password(payload.password),
        role=payload.role.value,
    )
    session.add(user)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Usuario ou e-mail ja cadastrado.") from exc
    await session.refresh(user)
    return user


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: UUID,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_roles("admin")),
) -> None:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Usuario nao encontrado.")
    if user.id == current_user.id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Voce nao pode excluir seu proprio usuario.")
    if user.role == "admin" and user.is_active:
        active_admins = await session.scalar(
            select(func.count()).select_from(User).where(User.role == "admin", User.is_active.is_(True))
        ) or 0
        if active_admins <= 1:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="O sistema deve manter ao menos um administrador ativo.",
            )
    await session.delete(user)
    await session.commit()


@router.patch("/{user_id}", response_model=UserRead)
async def update_user(
    user_id: UUID,
    payload: UserUpdate,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_roles("admin")),
) -> User:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Usuario nao encontrado.")

    data = payload.model_dump(exclude_unset=True)
    if user.id == current_user.id and data.get("is_active") is False:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Voce nao pode desativar seu proprio usuario.")
    requested_role = data.get("role")
    if user.id == current_user.id and requested_role is not None and requested_role.value != "admin":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Voce nao pode remover seu proprio perfil administrador.")
    if data.get("role") is not None:
        data["role"] = data["role"].value
    if data.get("email") is not None:
        data["email"] = str(data["email"]).lower()
    password = data.pop("password", None)
    if password:
        data["password_hash"] = hash_password(password)
    for field, value in data.items():
        setattr(user, field, value)

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="E-mail ja cadastrado.") from exc
    await session.refresh(user)
    return user
