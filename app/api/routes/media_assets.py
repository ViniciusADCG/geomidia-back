from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import and_, case, func, literal, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.routes.media_rules import active_rule_for_type, calculate_rule_radius
from app.core.security import require_roles
from app.db.models import ActivityLog, ApplicationForm, MediaAsset, MediaRule, ProcessCounter, User
from app.db.session import get_session
from app.domain.rules import AssetForAnalysis, evaluate_conflicts
from app.schemas import (
    ActivityType,
    ConflictAnalysisRead,
    MapAssetPage,
    MapAssetSummary,
    MapCluster,
    MapClusterPage,
    MediaAssetBase,
    MediaAssetCreate,
    MediaAssetPage,
    MediaAssetRead,
    MediaAssetUpdate,
    MediaExpirationOverviewRead,
    MediaStatsRead,
    MediaStatus,
    MediaType,
    RadiusReassessmentConfirm,
    RadiusReassessmentPreview,
)

router = APIRouter(prefix="/media-assets", tags=["media-assets"])
READ_ROLES = ("admin", "analyst", "viewer")
WRITE_ROLES = ("admin", "analyst")
APPROVAL_LOCK_ID = 729_041
EXPIRATION_WINDOW_DAYS = 90
MAP_MAX_ITEMS = 1000
BUSINESS_TIMEZONE = timezone(timedelta(hours=-4))
ANALYSIS_STATUS_VALUES = (
    MediaStatus.analysis.value,
    MediaStatus.exigency.value,
    MediaStatus.expired.value,
    MediaStatus.cartography.value,
    MediaStatus.legal.value,
    MediaStatus.inspection.value,
)


def expiration_window(reference_date: date | None = None) -> tuple[date, date]:
    start_date = reference_date or datetime.now(BUSINESS_TIMEZONE).date()
    return start_date, start_date + timedelta(days=EXPIRATION_WINDOW_DAYS)


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


def approval_conflict_confirmation_detail(analysis: dict[str, Any]) -> dict[str, Any]:
    return {
        "confirmation_required": True,
        "message": analysis["message"],
        "conflicts": analysis["conflicts"],
    }


def conflict_snapshots_match(
    current: list[dict[str, Any]],
    confirmed: list[dict[str, Any]] | None,
) -> bool:
    if confirmed is None:
        return False

    def key(item: dict[str, Any]) -> str:
        return str(item.get("conflicting_asset_id", ""))

    return sorted(current, key=key) == sorted(confirmed, key=key)


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
        "official_process_code": asset.official_process_code,
        "media_type": asset.media_type,
        "address": asset.address,
        "district": asset.district,
        "latitude": asset.latitude,
        "longitude": asset.longitude,
        "area_m2": asset.area_m2,
        "area_rule_classification": asset.area_rule_classification,
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
    application_form = asset.__dict__.get("application_form")
    if application_form is not None:
        serialized = serialized.model_copy(
            update={
                "company_responsible": application_form.company_responsible,
                "company_cnpj": application_form.company_cnpj,
            }
        )
    if user.role == "viewer":
        return serialized.model_copy(update={"contact_name": None, "contact_email": None})
    return serialized


async def get_asset_or_404(asset_id: UUID, session: AsyncSession) -> MediaAsset:
    asset = await session.get(MediaAsset, asset_id, options=(selectinload(MediaAsset.application_form),))
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


def company_match(value: str, *, partial: bool, include_manual: bool):
    """Match the linked form company, or a manual asset's visible contact name."""
    form_name = ApplicationForm.company_responsible.ilike(f"%{value}%") if partial else (
        func.lower(ApplicationForm.company_responsible) == value.lower()
    )
    matches = [MediaAsset.application_form.has(form_name)]
    if include_manual:
        manual_name = MediaAsset.contact_name.ilike(f"%{value}%") if partial else (
            func.lower(func.trim(MediaAsset.contact_name)) == value.lower()
        )
        matches.append(and_(~MediaAsset.application_form.has(), manual_name))
    return or_(*matches)


def media_asset_filters(
    *,
    search: str | None,
    media_type: MediaType | None,
    status_filter: MediaStatus | None,
    district: str | None,
    company: str | None,
    user_role: str,
):
    filters = []
    if search and (term := search.strip()):
        pattern = f"%{term}%"
        filters.append(
            or_(
                MediaAsset.address.ilike(pattern),
                MediaAsset.district.ilike(pattern),
                MediaAsset.process_code.ilike(pattern),
                MediaAsset.official_process_code.ilike(pattern),
                company_match(term, partial=True, include_manual=user_role != "viewer"),
            )
        )
    if media_type:
        filters.append(MediaAsset.media_type == media_type.value)
    if status_filter:
        filters.append(MediaAsset.status == status_filter.value)
    if district and (district_name := district.strip()):
        filters.append(func.lower(MediaAsset.district) == district_name.lower())
    if company and (company_name := company.strip()):
        filters.append(company_match(company_name, partial=False, include_manual=user_role != "viewer"))
    return filters


def map_spatial_match(north: float, south: float, east: float, west: float):
    """Test a WGS84 rectangle without treating its latitude edges as geodesic arcs."""
    viewport = func.ST_MakeEnvelope(west, south, east, north, 4326)
    point = func.Geography(MediaAsset.geom)

    def meridian(longitude: float):
        start = func.ST_SetSRID(func.ST_MakePoint(longitude, south), 4326)
        end = func.ST_SetSRID(func.ST_MakePoint(longitude, north), 4326)
        return func.Geography(func.ST_MakeLine(start, end))

    def parallel_nearest(latitude: float):
        nearest = func.ST_SetSRID(func.ST_MakePoint(MediaAsset.longitude, latitude), 4326)
        return and_(
            MediaAsset.longitude.between(west, east),
            func.ST_DWithin(point, func.Geography(nearest), MediaAsset.radius_meters),
        )

    # A point inside the rectangle qualifies. For a point outside, its nearest
    # point lies on a meridian side or on a parallel at the same longitude.
    return or_(
        MediaAsset.geom.op("&&")(viewport),
        func.ST_DWithin(point, meridian(west), MediaAsset.radius_meters),
        func.ST_DWithin(point, meridian(east), MediaAsset.radius_meters),
        parallel_nearest(north),
        parallel_nearest(south),
    )


async def map_index_prefilter(session: AsyncSession, viewport):
    """Use the existing geom GiST only when a conservative expansion is provable."""
    outside_campo_grande_region = or_(
        ~MediaAsset.latitude.between(-30, -10),
        ~MediaAsset.longitude.between(-60, -50),
    )
    max_radius, outliers = (
        await session.execute(
            select(
                func.max(MediaAsset.radius_meters),
                func.count().filter(outside_campo_grande_region),
            )
        )
    ).one()
    if max_radius is None or max_radius < 0 or outliers or max_radius > 100_000:
        return None

    # Around Campo Grande, one degree in either axis exceeds 50 km. For radii
    # up to 100 km, this expansion strictly contains every intersecting center.
    # Outlier coordinates or larger stored radii fall back to the exact test.
    return MediaAsset.geom.op("&&")(func.ST_Expand(viewport, max_radius / 50_000))


async def map_viewport_filters(
    session: AsyncSession,
    north: float,
    south: float,
    east: float,
    west: float,
    media_type: MediaType | None,
    status_filter: MediaStatus | None,
    company: str | None,
    user_role: str,
):
    if north <= south or east <= west:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A viewport exige north > south e east > west; antimeridiano não é suportado.",
        )
    viewport = func.ST_MakeEnvelope(west, south, east, north, 4326)
    filters = media_asset_filters(
        search=None,
        media_type=media_type,
        status_filter=status_filter,
        district=None,
        company=company,
        user_role=user_role,
    )
    prefilter = await map_index_prefilter(session, viewport)
    if prefilter is not None:
        filters.append(prefilter)
    filters.append(map_spatial_match(north, south, east, west))
    return filters


@router.get("", response_model=MediaAssetPage)
async def list_media_assets(
    search: str | None = Query(default=None, max_length=120),
    media_type: MediaType | None = Query(default=None),
    status_filter: MediaStatus | None = Query(default=None, alias="status"),
    district: str | None = Query(default=None, max_length=120),
    company: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_roles(*READ_ROLES)),
) -> MediaAssetPage:
    filters = media_asset_filters(
        search=search,
        media_type=media_type,
        status_filter=status_filter,
        district=district,
        company=company,
        user_role=current_user.role,
    )

    total = await session.scalar(select(func.count()).select_from(MediaAsset).where(*filters)) or 0
    result = await session.scalars(
        select(MediaAsset)
        .options(selectinload(MediaAsset.application_form))
        .where(*filters)
        .order_by(MediaAsset.created_at.desc(), MediaAsset.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return MediaAssetPage(
        items=[asset_for_user(asset, current_user) for asset in result],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/map", response_model=MapAssetPage)
async def list_map_assets(
    north: float = Query(ge=-90, le=90, allow_inf_nan=False),
    south: float = Query(ge=-90, le=90, allow_inf_nan=False),
    east: float = Query(ge=-180, le=180, allow_inf_nan=False),
    west: float = Query(ge=-180, le=180, allow_inf_nan=False),
    media_type: MediaType | None = Query(default=None),
    status_filter: MediaStatus | None = Query(default=None, alias="status"),
    company: str | None = Query(default=None, max_length=120),
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_roles(*READ_ROLES)),
) -> MapAssetPage:
    filters = await map_viewport_filters(
        session, north, south, east, west, media_type, status_filter, company, current_user.role,
    )

    total = await session.scalar(select(func.count()).select_from(MediaAsset).where(*filters)) or 0
    if total > MAP_MAX_ITEMS:
        return MapAssetPage(items=[], total=total, too_many=True, max_items=MAP_MAX_ITEMS)

    company_name = (
        select(ApplicationForm.company_responsible)
        .where(ApplicationForm.asset_id == MediaAsset.id)
        .scalar_subquery()
    )
    manual_name = case((~MediaAsset.application_form.has(), MediaAsset.contact_name), else_=None)
    contact_name = manual_name if current_user.role != "viewer" else literal(None)
    rows = await session.execute(
        select(
            MediaAsset.id,
            MediaAsset.latitude,
            MediaAsset.longitude,
            MediaAsset.radius_meters,
            MediaAsset.media_type,
            MediaAsset.status,
            MediaAsset.process_code,
            MediaAsset.official_process_code,
            company_name.label("company_responsible"),
            contact_name.label("contact_name"),
        )
        .where(*filters)
        .order_by(MediaAsset.created_at.desc(), MediaAsset.id.desc())
    )
    return MapAssetPage(
        items=[MapAssetSummary.model_validate(dict(row._mapping)) for row in rows],
        total=total,
        too_many=False,
        max_items=MAP_MAX_ITEMS,
    )


@router.get("/map/clusters", response_model=MapClusterPage)
async def list_map_clusters(
    north: float = Query(ge=-90, le=90, allow_inf_nan=False),
    south: float = Query(ge=-90, le=90, allow_inf_nan=False),
    east: float = Query(ge=-180, le=180, allow_inf_nan=False),
    west: float = Query(ge=-180, le=180, allow_inf_nan=False),
    zoom: int = Query(ge=0, le=22),
    media_type: MediaType | None = Query(default=None),
    status_filter: MediaStatus | None = Query(default=None, alias="status"),
    company: str | None = Query(default=None, max_length=120),
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_roles(*READ_ROLES)),
) -> MapClusterPage:
    filters = await map_viewport_filters(
        session, north, south, east, west, media_type, status_filter, company, current_user.role,
    )
    # A grade ocupa no máximo 400 células, independentemente do número de ativos.
    # O tamanho geográfico da célula diminui ao aproximar a viewport.
    cells = 12 if zoom <= 10 else 16 if zoom <= 13 else 20
    cell_width = (east - west) / cells
    cell_height = (north - south) / cells
    column = func.least(cells - 1, func.greatest(0, func.floor((MediaAsset.longitude - west) / cell_width)))
    row = func.least(cells - 1, func.greatest(0, func.floor((MediaAsset.latitude - south) / cell_height)))
    grouped = await session.execute(
        select(column.label("column"), row.label("row"), func.count(MediaAsset.id).label("count"))
        .where(*filters)
        .group_by(column, row)
        .order_by(column, row)
    )
    clusters = [
        MapCluster(
            id=f"{zoom}:{int(item._mapping['column'])}:{int(item._mapping['row'])}",
            count=item._mapping["count"],
            latitude=south + (item._mapping["row"] + 0.5) * cell_height,
            longitude=west + (item._mapping["column"] + 0.5) * cell_width,
        )
        for item in grouped
    ]
    return MapClusterPage(clusters=clusters, total=sum(cluster.count for cluster in clusters))


@router.get("/map/company-options", response_model=list[str])
async def list_map_company_options(
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_roles(*READ_ROLES)),
) -> list[str]:
    linked = await session.scalars(select(ApplicationForm.company_responsible).distinct())
    names = {name.strip() for name in linked if name and name.strip()}
    if current_user.role != "viewer":
        manual = await session.scalars(
            select(MediaAsset.contact_name)
            .where(~MediaAsset.application_form.has(), MediaAsset.contact_name.is_not(None))
            .distinct()
        )
        names.update(name.strip() for name in manual if name and name.strip())
    return sorted(names, key=lambda name: (name.casefold(), name))


@router.get("/stats", response_model=MediaStatsRead)
async def get_media_stats(
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_roles(*READ_ROLES)),
) -> MediaStatsRead:
    reference_date, window_end_date = expiration_window()
    summary = (
        await session.execute(
            select(
                func.count(MediaAsset.id),
                func.count(MediaAsset.id).filter(MediaAsset.status == MediaStatus.new_process.value),
                func.count(MediaAsset.id).filter(MediaAsset.status.in_(ANALYSIS_STATUS_VALUES)),
                func.count(MediaAsset.id).filter(MediaAsset.status == MediaStatus.approved.value),
                func.count(MediaAsset.id).filter(MediaAsset.status == MediaStatus.irregular.value),
                func.count(MediaAsset.id).filter(
                    MediaAsset.expiration_date.between(reference_date, window_end_date)
                ),
                func.count(MediaAsset.id).filter(MediaAsset.expiration_date < reference_date),
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
        expiring_soon=summary[5],
        expired=summary[6],
        by_type={media_type: count for media_type, count in by_type_rows},
    )


@router.get("/expirations", response_model=MediaExpirationOverviewRead)
async def get_media_expirations(
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_roles(*READ_ROLES)),
) -> MediaExpirationOverviewRead:
    reference_date, window_end_date = expiration_window()
    expiring_soon = await session.scalars(
        select(MediaAsset)
        .options(selectinload(MediaAsset.application_form))
        .where(MediaAsset.expiration_date.between(reference_date, window_end_date))
        .order_by(MediaAsset.expiration_date.asc(), MediaAsset.process_code.asc())
    )
    expired = await session.scalars(
        select(MediaAsset)
        .options(selectinload(MediaAsset.application_form))
        .where(MediaAsset.expiration_date < reference_date)
        .order_by(MediaAsset.expiration_date.desc(), MediaAsset.process_code.asc())
    )
    return MediaExpirationOverviewRead(
        reference_date=reference_date,
        window_end_date=window_end_date,
        expiring_soon=[asset_for_user(asset, current_user) for asset in expiring_soon],
        expired=[asset_for_user(asset, current_user) for asset in expired],
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
    await session.refresh(asset, attribute_names=["application_form"])
    return asset_for_user(asset, current_user)


@router.post("", response_model=MediaAssetRead, status_code=status.HTTP_201_CREATED)
async def create_media_asset(
    payload: MediaAssetCreate,
    request: Request,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
) -> MediaAssetRead:
    data = payload.model_dump(mode="json")
    data["expiration_date"] = payload.expiration_date
    data["process_code"] = await next_process_code(session)
    rule = await active_rule_for_type(payload.media_type.value, session)
    data["radius_meters"] = calculate_rule_radius(rule, payload.area_m2, payload.area_rule_classification)

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
    return asset_for_user(asset, current_user)


def reassessment_preview(asset: MediaAsset, rule: MediaRule | None) -> RadiusReassessmentPreview:
    condition = "area_m2 real" if asset.area_m2 is not None else (
        "classificacao historica" if asset.area_rule_classification else "sem informacao de area"
    )
    unavailable = None
    if rule is None or not rule.is_active:
        unavailable = "Nao existe regra ativa para este tipo de veiculo."
    elif rule.area_threshold_m2 is not None and asset.area_m2 is None:
        unavailable = (
            "A classificacao anterior nao pode ser reinterpretada sob o limite atual; "
            "obtenha nova informacao verificavel de area."
            if asset.area_rule_classification else
            "A regra atual exige informacao verificavel de area."
        )
    radius = None if unavailable else calculate_rule_radius(rule, asset.area_m2)
    return RadiusReassessmentPreview(
        asset_id=asset.id,
        registered_radius_meters=asset.radius_meters,
        current_radius_meters=radius,
        condition=condition,
        can_reassess=unavailable is None,
        reason_unavailable=unavailable,
        rule_id=rule.id if rule else None,
        rule_updated_at=rule.updated_at if rule else None,
        rule_base_radius_meters=rule.base_radius_meters if rule else None,
        rule_area_threshold_m2=rule.area_threshold_m2 if rule else None,
        rule_radius_above_threshold_meters=rule.radius_above_threshold_meters if rule else None,
    )


@router.get("/{asset_id}/radius-reassessment", response_model=RadiusReassessmentPreview)
async def preview_radius_reassessment(
    asset_id: UUID,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_roles("admin")),
) -> RadiusReassessmentPreview:
    asset = await get_asset_or_404(asset_id, session)
    rule = await session.scalar(select(MediaRule).where(MediaRule.media_type == asset.media_type))
    return reassessment_preview(asset, rule)


@router.post("/{asset_id}/radius-reassessment", response_model=MediaAssetRead)
async def confirm_radius_reassessment(
    asset_id: UUID,
    payload: RadiusReassessmentConfirm,
    request: Request,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_roles("admin")),
) -> MediaAssetRead:
    asset = await session.scalar(select(MediaAsset).where(MediaAsset.id == asset_id).with_for_update())
    if asset is None:
        raise HTTPException(status_code=404, detail="Ativo nao encontrado.")
    rule = await session.scalar(select(MediaRule).where(MediaRule.media_type == asset.media_type).with_for_update())
    preview = reassessment_preview(asset, rule)
    if not preview.can_reassess:
        raise HTTPException(status_code=422, detail=preview.reason_unavailable)
    if (asset.radius_meters != payload.expected_registered_radius_meters
            or rule.updated_at != payload.expected_rule_updated_at):
        raise HTTPException(status_code=409, detail="Ativo ou regra mudou. Abra a previa novamente.")
    previous_radius = asset.radius_meters
    asset.radius_meters = preview.current_radius_meters
    session.add(log_activity(
        asset, ActivityType.edicao, f"Raio do processo {asset.process_code} reavaliado.",
        current_user, request.state.request_id,
        {
            "radius_meters": {"before": previous_radius, "after": asset.radius_meters},
            "reason": payload.reason,
            "rule": {
                "id": str(rule.id), "updated_at": rule.updated_at.isoformat(),
                "base_radius_meters": rule.base_radius_meters,
                "area_threshold_m2": rule.area_threshold_m2,
                "radius_above_threshold_meters": rule.radius_above_threshold_meters,
            },
            "condition": preview.condition,
        },
    ))
    await session.commit()
    await session.refresh(asset)
    return asset_for_user(asset, current_user)


@router.patch("/{asset_id}", response_model=MediaAssetRead)
async def update_media_asset(
    asset_id: UUID,
    payload: MediaAssetUpdate,
    request: Request,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
) -> MediaAssetRead:
    asset = await get_asset_or_404(asset_id, session)
    before = asset_snapshot(asset)
    update_data = payload.model_dump(exclude_unset=True, mode="json")
    confirm_conflict_approval = update_data.pop("confirm_conflict_approval", False)
    confirmed_conflicts = update_data.pop("confirmed_conflicts", None)
    if "official_process_code" in update_data:
        update_data["official_process_code"] = (update_data["official_process_code"] or "").strip() or None
    official_process_code = update_data.pop("official_process_code", asset.official_process_code)
    ensure_direct_status_change_allowed(asset.status, update_data.get("status"))

    required_fields = {"media_type", "address", "district", "latitude", "longitude", "status"}
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
    asset.official_process_code = official_process_code
    spatial_fields = {"media_type", "latitude", "longitude", "area_m2", "area_rule_classification"}
    requires_approval_review = (
        asset.status == MediaStatus.approved.value
        and (
            before["status"] != MediaStatus.approved.value
            or bool(spatial_fields.intersection(update_data))
        )
    )
    approved_conflicts: list[dict[str, Any]] = []
    if requires_approval_review:
        await session.execute(select(func.pg_advisory_xact_lock(APPROVAL_LOCK_ID)))
        await session.flush()
        analysis = await analyze_asset(asset, session)
        if analysis["has_conflict"]:
            if not confirm_conflict_approval or not conflict_snapshots_match(analysis["conflicts"], confirmed_conflicts):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=approval_conflict_confirmation_detail(analysis),
                )
            approved_conflicts = analysis["conflicts"]

    after = asset_snapshot(asset)
    changed = {
        field: {"before": before.get(field), "after": value}
        for field, value in after.items()
        if before.get(field) != value
    }
    if approved_conflicts:
        changed["conflict_approval"] = {"confirmed": True, "conflicts": approved_conflicts}
    if not changed:
        return asset_for_user(asset, current_user)

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
    await session.refresh(asset, attribute_names=["application_form"])
    return asset_for_user(asset, current_user)


@router.delete("/{asset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_media_asset(
    asset_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
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
