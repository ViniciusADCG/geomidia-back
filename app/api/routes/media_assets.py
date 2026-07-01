from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ActivityLog, MediaAsset
from app.db.session import get_session
from app.domain.rules import AssetForAnalysis, evaluate_conflicts, get_required_radius
from app.schemas import (
    ActivityType,
    ConflictAnalysisRead,
    MediaAssetCreate,
    MediaAssetRead,
    MediaAssetUpdate,
    MediaStatsRead,
)


router = APIRouter(prefix="/media-assets", tags=["media-assets"])


def to_analysis_asset(asset: MediaAsset) -> AssetForAnalysis:
    return AssetForAnalysis(
        id=asset.id,
        process_code=asset.process_code,
        media_type=asset.media_type,
        status=asset.status,
        latitude=asset.latitude,
        longitude=asset.longitude,
        area_m2=asset.area_m2,
        radius_meters=asset.radius_meters,
    )


async def get_asset_or_404(asset_id: UUID, session: AsyncSession) -> MediaAsset:
    asset = await session.get(MediaAsset, asset_id)
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ativo nao encontrado.")
    return asset


async def next_process_code(session: AsyncSession) -> str:
    year = datetime.now().year
    prefix = f"PROC-{year}-"
    count = await session.scalar(
        select(func.count()).select_from(MediaAsset).where(MediaAsset.process_code.like(f"{prefix}%"))
    )
    return f"{prefix}{(count or 0) + 101:03d}"


def log_activity(asset: MediaAsset, activity_type: ActivityType, message: str) -> ActivityLog:
    return ActivityLog(
        asset_id=asset.id,
        process_code=asset.process_code,
        activity_type=activity_type.value,
        message=message,
    )


@router.get("", response_model=list[MediaAssetRead])
async def list_media_assets(
    search: str | None = None,
    media_type: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    session: AsyncSession = Depends(get_session),
) -> list[MediaAsset]:
    stmt = select(MediaAsset).order_by(MediaAsset.created_at.desc())

    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(
            or_(
                MediaAsset.address.ilike(pattern),
                MediaAsset.district.ilike(pattern),
                MediaAsset.process_code.ilike(pattern),
            )
        )
    if media_type:
        stmt = stmt.where(MediaAsset.media_type == media_type)
    if status_filter:
        stmt = stmt.where(MediaAsset.status == status_filter)

    result = await session.scalars(stmt)
    return list(result)


@router.get("/stats", response_model=MediaStatsRead)
async def get_media_stats(session: AsyncSession = Depends(get_session)) -> MediaStatsRead:
    assets = list(await session.scalars(select(MediaAsset)))
    by_type: dict[str, int] = {}
    for asset in assets:
        by_type[asset.media_type] = by_type.get(asset.media_type, 0) + 1

    return MediaStatsRead(
        total=len(assets),
        pending=sum(1 for asset in assets if asset.status == "Pendente"),
        approved=sum(1 for asset in assets if asset.status == "Aprovado"),
        rejected=sum(1 for asset in assets if asset.status == "Reprovado"),
        by_type=by_type,
    )


@router.get("/{asset_id}", response_model=MediaAssetRead)
async def get_media_asset(asset_id: UUID, session: AsyncSession = Depends(get_session)) -> MediaAsset:
    return await get_asset_or_404(asset_id, session)


@router.get("/{asset_id}/analysis", response_model=ConflictAnalysisRead)
async def analyze_media_asset(asset_id: UUID, session: AsyncSession = Depends(get_session)) -> dict:
    asset = await get_asset_or_404(asset_id, session)
    candidate_point = func.ST_SetSRID(func.ST_MakePoint(asset.longitude, asset.latitude), 4326)
    result = await session.scalars(
        select(MediaAsset).where(
            MediaAsset.id != asset_id,
            MediaAsset.status != "Reprovado",
            func.ST_DWithin(func.Geography(MediaAsset.geom), func.Geography(candidate_point), 2000),
        )
    )
    analysis = evaluate_conflicts(to_analysis_asset(asset), [to_analysis_asset(item) for item in result])
    return analysis


@router.post("", response_model=MediaAssetRead, status_code=status.HTTP_201_CREATED)
async def create_media_asset(
    payload: MediaAssetCreate,
    session: AsyncSession = Depends(get_session),
) -> MediaAsset:
    data = payload.model_dump()
    data["media_type"] = payload.media_type.value
    data["status"] = payload.status.value
    data["process_code"] = await next_process_code(session)
    data["radius_meters"] = get_required_radius(payload.media_type.value, payload.area_m2)

    asset = MediaAsset(**data)
    session.add(asset)
    await session.flush()

    session.add(
        log_activity(
            asset,
            ActivityType.cadastro,
            f"Cadastro solicitado para {asset.media_type.upper()} em {asset.address}.",
        )
    )
    await session.commit()
    await session.refresh(asset)
    return asset


@router.patch("/{asset_id}", response_model=MediaAssetRead)
async def update_media_asset(
    asset_id: UUID,
    payload: MediaAssetUpdate,
    session: AsyncSession = Depends(get_session),
) -> MediaAsset:
    asset = await get_asset_or_404(asset_id, session)
    previous_status = asset.status

    update_data = payload.model_dump(exclude_unset=True)
    if "media_type" in update_data and update_data["media_type"] is not None:
        update_data["media_type"] = update_data["media_type"].value
    if "status" in update_data and update_data["status"] is not None:
        update_data["status"] = update_data["status"].value

    for field, value in update_data.items():
        setattr(asset, field, value)

    asset.radius_meters = get_required_radius(asset.media_type, asset.area_m2)
    await session.flush()

    if previous_status != asset.status:
        activity_type = ActivityType.aprovacao if asset.status == "Aprovado" else ActivityType.reprovacao
        message = f"Processo {asset.process_code} alterado para {asset.status}. {asset.justification or ''}".strip()
    else:
        activity_type = ActivityType.edicao
        message = f"Dados cadastrais do processo {asset.process_code} foram atualizados."

    session.add(log_activity(asset, activity_type, message))
    await session.commit()
    await session.refresh(asset)
    return asset


@router.delete("/{asset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_media_asset(asset_id: UUID, session: AsyncSession = Depends(get_session)) -> None:
    asset = await get_asset_or_404(asset_id, session)
    session.add(
        ActivityLog(
            asset_id=None,
            process_code=asset.process_code,
            activity_type=ActivityType.remocao.value,
            message=f"Registro {asset.process_code} removido do inventario municipal.",
        )
    )
    await session.delete(asset)
    await session.commit()
