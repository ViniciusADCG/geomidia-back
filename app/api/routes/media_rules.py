from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import require_roles
from app.db.models import MediaAsset, MediaRule, User
from app.db.session import get_session
from app.schemas import MediaRuleBase, MediaRuleCreate, MediaRuleRead, MediaRuleUpdate

router = APIRouter(prefix="/media-rules", tags=["media-rules"])
MEDIA_RULE_READ_ROLES = ("admin", "analyst", "viewer")


def calculate_rule_radius(rule: MediaRule, area_m2: float) -> int:
    if (
        rule.area_threshold_m2 is not None
        and rule.radius_above_threshold_meters is not None
        and area_m2 > rule.area_threshold_m2
    ):
        return rule.radius_above_threshold_meters
    return rule.base_radius_meters


async def active_rule_for_type(media_type: str, session: AsyncSession) -> MediaRule:
    rule = await session.scalar(
        select(MediaRule).where(MediaRule.media_type == media_type, MediaRule.is_active.is_(True))
    )
    if rule is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Nao existe regra ativa para o tipo de veiculo '{media_type}'.",
        )
    return rule


async def refresh_asset_radii(rule: MediaRule, session: AsyncSession) -> None:
    assets = await session.scalars(select(MediaAsset).where(MediaAsset.media_type == rule.media_type))
    for asset in assets:
        asset.radius_meters = calculate_rule_radius(rule, asset.area_m2)


async def get_rule_or_404(rule_id: UUID, session: AsyncSession) -> MediaRule:
    rule = await session.get(MediaRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Regra de negocio nao encontrada.")
    return rule


@router.get("", response_model=list[MediaRuleRead])
async def list_media_rules(
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_roles(*MEDIA_RULE_READ_ROLES)),
) -> list[MediaRule]:
    return list(await session.scalars(select(MediaRule).order_by(MediaRule.name)))


@router.post("", response_model=MediaRuleRead, status_code=status.HTTP_201_CREATED)
async def create_media_rule(
    payload: MediaRuleCreate,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_roles("admin")),
) -> MediaRule:
    rule = MediaRule(**payload.model_dump(mode="json"))
    session.add(rule)
    try:
        await session.flush()
        await refresh_asset_radii(rule, session)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Ja existe uma regra para esse tipo de veiculo.",
        ) from exc
    await session.refresh(rule)
    return rule


@router.patch("/{rule_id}", response_model=MediaRuleRead)
async def update_media_rule(
    rule_id: UUID,
    payload: MediaRuleUpdate,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_roles("admin")),
) -> MediaRule:
    rule = await get_rule_or_404(rule_id, session)
    before = MediaRuleBase.model_validate(rule).model_dump(mode="json")
    changes = payload.model_dump(exclude_unset=True, mode="json")
    merged = {**before, **changes}
    validated = MediaRuleBase.model_validate(merged)

    for field, value in validated.model_dump(mode="json").items():
        setattr(rule, field, value)
    await refresh_asset_radii(rule, session)
    await session.commit()
    await session.refresh(rule)
    return rule


@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_media_rule(
    rule_id: UUID,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_roles("admin")),
) -> None:
    rule = await get_rule_or_404(rule_id, session)
    asset_using_rule = await session.scalar(select(MediaAsset.id).where(MediaAsset.media_type == rule.media_type).limit(1))
    if asset_using_rule is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A regra nao pode ser excluida enquanto houver veiculos desse tipo. Desative-a ou altere os cadastros.",
        )
    await session.delete(rule)
    await session.commit()
