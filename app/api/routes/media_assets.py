from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.media_rules import active_rule_for_type, calculate_rule_radius
from app.core.security import require_roles
from app.db.models import ActivityLog, MediaAsset, ProcessCounter, User
from app.db.session import get_session
from app.domain.rules import AssetForAnalysis, evaluate_conflicts
from app.schemas import (
    ActivityType,
    ConflictAnalysisRead,
    MediaAssetBase,
    MediaAssetCreate,
    MediaAssetPage,
    MediaAssetRead,
    MediaAssetUpdate,
    MediaStatsRead,
    MediaStatus,
    MediaType,
)

router = APIRouter(prefix="/media-assets", tags=["media-assets"])
READ_ROLES = ("admin", "analyst", "viewer")
WRITE_ROLES = ("admin", "analyst")
APPROVAL_LOCK_ID = 729_041
ANALYSIS_STATUS_VALUES = (
    MediaStatus.analysis.value,
    MediaStatus.exigency.value,
    MediaStatus.expired.value,
    MediaStatus.cartography.value,
    MediaStatus.legal.value,
    MediaStatus.inspection.value,
)


def ensure_direct_status_change_allowed(current_status: str, requested_status: str | None) -> None:
    if requested_status is None or requested_status == current_status:
        return
    if current_status == MediaStatus.new_process.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Inicie a análise do novo processo antes de modificar seu status.",
        )
    if requested_status == MediaStatus.new_process.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Novos Processos é um status exclusivo de novos cadastros.",
        )


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


def asset_snapshot(asset: MediaAsset) -> dict[str, Any]:
    return {
        "media_type": asset.media_type,
        "address": asset.address,
        "district": asset.district,
        "latitude": asset.latitude,
        "longitude": asset.longitude,
        "area_m2": asset.area_m2,
        "width_m": asset.width_m,
        "bottom_height_m": asset.bottom_height_m,
        "top_height_m": asset.top_height_m,
        "expiration_date": asset.expiration_date.isoformat() if asset.expiration_date else None,
        "status": asset.status,
        "justification": asset.justification,
        "attachment_links": asset.attachment_links,
        "contact_name": asset.contact_name,
        "contact_email": asset.contact_email,
        "radius_meters": asset.radius_meters,
    }


def asset_for_user(asset: MediaAsset, user: User) -> MediaAssetRead:
    serialized = MediaAssetRead.model_validate(asset)
    if user.role == "viewer":
        return serialized.model_copy(update={"contact_name": None, "contact_email": None})
    return serialized


async def get_asset_or_404(asset_id: UUID, session: AsyncSession) -> MediaAsset:
    asset = await session.get(MediaAsset, asset_id)
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ativo nao encontrado.")
    return asset


async def next_process_code(session: AsyncSession) -> str:
    year = datetime.now(UTC).year
    prefix = f"PROC-{year}-"
    existing_codes = await session.scalars(
        select(MediaAsset.process_code).where(MediaAsset.process_code.like(f"{prefix}%"))
    )
    existing_numbers: list[int] = []
    for code in existing_codes:
        try:
            existing_numbers.append(int(code.rsplit("-", 1)[1]))
        except (IndexError, ValueError):
            continue
    first_value = max(existing_numbers, default=100) + 1

    statement = (
        pg_insert(ProcessCounter)
        .values(year=year, last_value=first_value)
        .on_conflict_do_update(
            index_elements=[ProcessCounter.year],
            set_={"last_value": ProcessCounter.last_value + 1},
        )
        .returning(ProcessCounter.last_value)
    )
    sequence_value = await session.scalar(statement)
    return f"{prefix}{sequence_value:03d}"


def log_activity(
    asset: MediaAsset,
    activity_type: ActivityType,
    message: str,
    actor: User | None = None,
    request_id: str | None = None,
    changes: dict[str, Any] | None = None,
) -> ActivityLog:
    return ActivityLog(
        asset_id=asset.id,
        actor_user_id=actor.id if actor else None,
        process_code=asset.process_code,
        activity_type=activity_type.value,
        message=message,
        request_id=request_id,
        changes=changes,
    )


async def analyze_asset(asset: MediaAsset, session: AsyncSession) -> dict[str, Any]:
    candidate_point = func.ST_SetSRID(func.ST_MakePoint(asset.longitude, asset.latitude), 4326)
    largest_radius = await session.scalar(
        select(func.max(MediaAsset.radius_meters)).where(MediaAsset.status != MediaStatus.irregular.value)
    ) or 0
    search_radius = max(asset.radius_meters, largest_radius, 500)
    result = await session.scalars(
        select(MediaAsset).where(
            MediaAsset.id != asset.id,
            MediaAsset.status != MediaStatus.irregular.value,
            func.ST_DWithin(func.Geography(MediaAsset.geom), func.Geography(candidate_point), search_radius),
        )
    )
    return evaluate_conflicts(to_analysis_asset(asset), [to_analysis_asset(item) for item in result])


@router.get("", response_model=MediaAssetPage)
async def list_media_assets(
    search: str | None = Query(default=None, max_length=120),
    media_type: MediaType | None = Query(default=None),
    status_filter: MediaStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_roles(*READ_ROLES)),
) -> MediaAssetPage:
    filters = []
    if search:
        pattern = f"%{search.strip()}%"
        filters.append(
            or_(
                MediaAsset.address.ilike(pattern),
                MediaAsset.district.ilike(pattern),
                MediaAsset.process_code.ilike(pattern),
            )
        )
    if media_type:
        filters.append(MediaAsset.media_type == media_type.value)
    if status_filter:
        filters.append(MediaAsset.status == status_filter.value)

    total = await session.scalar(select(func.count()).select_from(MediaAsset).where(*filters)) or 0
    result = await session.scalars(
        select(MediaAsset)
        .where(*filters)
        .order_by(MediaAsset.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    return MediaAssetPage(
        items=[asset_for_user(asset, current_user) for asset in result],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/stats", response_model=MediaStatsRead)
async def get_media_stats(
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_roles(*READ_ROLES)),
) -> MediaStatsRead:
    summary = (
        await session.execute(
            select(
                func.count(MediaAsset.id),
                func.count(MediaAsset.id).filter(MediaAsset.status == MediaStatus.new_process.value),
                func.count(MediaAsset.id).filter(MediaAsset.status.in_(ANALYSIS_STATUS_VALUES)),
                func.count(MediaAsset.id).filter(MediaAsset.status == MediaStatus.approved.value),
                func.count(MediaAsset.id).filter(MediaAsset.status == MediaStatus.irregular.value),
            )
        )
    ).one()
    by_type_rows = await session.execute(
        select(MediaAsset.media_type, func.count(MediaAsset.id)).group_by(MediaAsset.media_type)
    )
    return MediaStatsRead(
        total=summary[0],
        new_processes=summary[1],
        pending=summary[2],
        approved=summary[3],
        rejected=summary[4],
        by_type={media_type: count for media_type, count in by_type_rows},
    )


@router.get("/{asset_id}", response_model=MediaAssetRead)
async def get_media_asset(
    asset_id: UUID,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_roles(*READ_ROLES)),
) -> MediaAssetRead:
    return asset_for_user(await get_asset_or_404(asset_id, session), current_user)


@router.get("/{asset_id}/analysis", response_model=ConflictAnalysisRead)
async def analyze_media_asset(
    asset_id: UUID,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_roles(*READ_ROLES)),
) -> dict[str, Any]:
    asset = await get_asset_or_404(asset_id, session)
    if asset.status == MediaStatus.new_process.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Inicie a análise do novo processo antes de calcular conflitos.",
        )
    return await analyze_asset(asset, session)


@router.post("/{asset_id}/start-analysis", response_model=MediaAssetRead)
async def start_media_asset_analysis(
    asset_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
) -> MediaAssetRead:
    asset = await get_asset_or_404(asset_id, session)
    if asset.status != MediaStatus.new_process.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Somente processos com status Novos Processos podem iniciar a análise.",
        )

    previous_status = asset.status
    asset.status = MediaStatus.analysis.value
    session.add(
        log_activity(
            asset,
            ActivityType.edicao,
            f"Análise do processo {asset.process_code} iniciada.",
            current_user,
            request.state.request_id,
            {"status": {"before": previous_status, "after": asset.status}},
        )
    )
    await session.commit()
    await session.refresh(asset)
    return asset_for_user(asset, current_user)


@router.post("", response_model=MediaAssetRead, status_code=status.HTTP_201_CREATED)
async def create_media_asset(
    payload: MediaAssetCreate,
    request: Request,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
) -> MediaAsset:
    data = payload.model_dump(mode="json")
    data["expiration_date"] = payload.expiration_date
    data["process_code"] = await next_process_code(session)
    rule = await active_rule_for_type(payload.media_type.value, session)
    data["radius_meters"] = calculate_rule_radius(rule, payload.area_m2)

    asset = MediaAsset(**data)
    session.add(asset)
    await session.flush()
    session.add(
        log_activity(
            asset,
            ActivityType.cadastro,
            f"Cadastro solicitado para {asset.media_type.upper()} em {asset.address}.",
            current_user,
            request.state.request_id,
            {"after": asset_snapshot(asset)},
        )
    )
    await session.commit()
    await session.refresh(asset)
    return asset


@router.patch("/{asset_id}", response_model=MediaAssetRead)
async def update_media_asset(
    asset_id: UUID,
    payload: MediaAssetUpdate,
    request: Request,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
) -> MediaAsset:
    asset = await get_asset_or_404(asset_id, session)
    before = asset_snapshot(asset)
    update_data = payload.model_dump(exclude_unset=True, mode="json")
    ensure_direct_status_change_allowed(asset.status, update_data.get("status"))

    required_fields = {"media_type", "address", "district", "latitude", "longitude", "area_m2", "bottom_height_m", "status"}
    invalid_nulls = required_fields.intersection(field for field, value in update_data.items() if value is None)
    if invalid_nulls:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Campos obrigatorios nao podem ser nulos: {', '.join(sorted(invalid_nulls))}.",
        )

    merged = {key: before[key] for key in MediaAssetBase.model_fields}
    merged.update(update_data)
    validated = MediaAssetBase.model_validate(merged)
    final_data = validated.model_dump(mode="json")
    final_data["expiration_date"] = validated.expiration_date
    if validated.status == MediaStatus.irregular and not (validated.justification or "").strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A justificativa e obrigatoria para marcar um processo como irregular.",
        )

    for field, value in final_data.items():
        setattr(asset, field, value)
    rule = await active_rule_for_type(asset.media_type, session)
    asset.radius_meters = calculate_rule_radius(rule, asset.area_m2)

    if asset.status == MediaStatus.approved.value:
        await session.execute(select(func.pg_advisory_xact_lock(APPROVAL_LOCK_ID)))
        await session.flush()
        analysis = await analyze_asset(asset, session)
        if analysis["has_conflict"]:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"message": analysis["message"], "conflicts": analysis["conflicts"]},
            )

    after = asset_snapshot(asset)
    changed = {
        field: {"before": before.get(field), "after": value}
        for field, value in after.items()
        if before.get(field) != value
    }
    if not changed:
        return asset

    previous_status = before["status"]
    if previous_status != asset.status and asset.status == MediaStatus.approved.value:
        activity_type = ActivityType.aprovacao
        message = f"Processo {asset.process_code} aprovado. {asset.justification or ''}".strip()
    elif previous_status != asset.status and asset.status == MediaStatus.irregular.value:
        activity_type = ActivityType.reprovacao
        message = f"Processo {asset.process_code} marcado como irregular. {asset.justification or ''}".strip()
    else:
        activity_type = ActivityType.edicao
        message = f"Dados do processo {asset.process_code} foram atualizados."

    session.add(
        log_activity(
            asset,
            activity_type,
            message,
            current_user,
            request.state.request_id,
            changed,
        )
    )
    await session.commit()
    await session.refresh(asset)
    return asset


@router.delete("/{asset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_media_asset(
    asset_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_roles("admin")),
) -> None:
    asset = await get_asset_or_404(asset_id, session)
    session.add(
        ActivityLog(
            asset_id=None,
            actor_user_id=current_user.id,
            process_code=asset.process_code,
            activity_type=ActivityType.remocao.value,
            message=f"Registro {asset.process_code} removido do inventario municipal.",
            request_id=request.state.request_id,
            changes={"before": asset_snapshot(asset), "after": None},
        )
    )
    await session.delete(asset)
    await session.commit()
