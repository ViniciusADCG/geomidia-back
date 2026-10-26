"""Administrator-only process timelines and elapsed calendar time."""

from datetime import UTC, date, datetime, time, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.security import require_roles
from app.db.models import ActivityLog, MediaAsset, User
from app.db.session import get_session
from app.schemas import MediaStatus

router = APIRouter(prefix="/process-tracking", tags=["process-tracking"])
BUSINESS_TIMEZONE = timezone(timedelta(hours=-4))


class TimelineEvent(BaseModel):
    at: datetime
    before: str | None
    after: str | None
    analyst_id: UUID | None
    analyst_name: str | None
    analyst_role_inferred: bool = False
    is_entry: bool = False
    is_deletion: bool = False


class TrackedProcess(BaseModel):
    asset_id: UUID | None
    process_code: str
    official_process_code: str | None
    current_status: str | None
    created_at: datetime | None
    deleted_at: datetime | None = None
    deletion_reason: str | None = None
    analysis_started_at: datetime | None
    first_approved_at: datetime | None
    seconds_to_analysis: int | None
    seconds_analysis_to_approval: int | None
    seconds_to_approval: int | None
    seconds_in_current_status: int | None
    timeline: list[TimelineEvent]


class TrackingPage(BaseModel):
    items: list[TrackedProcess]
    total: int
    limit: int
    offset: int


class TrackedAnalyst(BaseModel):
    id: UUID
    name: str


class AnalystActivitySummary(BaseModel):
    id: UUID
    name: str
    processes_touched: int
    actions: int
    analyses_started: int
    approvals: int


def analyst_identity(log: ActivityLog, current_user: User | None) -> tuple[UUID | None, str | None, bool]:
    """Older logs have no role snapshot; their current user role is only an inference."""
    if log.actor_role == "analyst":
        return log.actor_user_id, log.actor_name or (current_user.full_name if current_user else None), False
    if log.actor_role is None and current_user is not None and current_user.role == "analyst":
        return log.actor_user_id, current_user.full_name, True
    return None, None, False


def status_change(log: ActivityLog) -> tuple[str | None, str] | None:
    changes = log.changes or {}
    asset_changes = changes.get("asset") if isinstance(changes.get("asset"), dict) else changes
    transition = asset_changes.get("status") if isinstance(asset_changes, dict) else None
    if not isinstance(transition, dict):
        return None
    before, after = transition.get("before"), transition.get("after")
    if not isinstance(after, str) or before == after:
        return None
    return before if isinstance(before, str) else None, after


def elapsed(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None:
        return None
    return max(0, int((end - start).total_seconds()))


def tracked_process(
    asset: MediaAsset,
    logs: list[tuple[ActivityLog, User | None]],
    now: datetime,
    deletion: ActivityLog | None = None,
) -> TrackedProcess:
    registration = next((log for log, _ in logs if log.activity_type == "cadastro"), None)
    initial_status = ((registration.changes or {}).get("after") or {}).get("status") if registration else None
    if not isinstance(initial_status, str) and asset.status == MediaStatus.new_process.value:
        initial_status = asset.status
    timeline = []
    if asset.created_at:
        timeline.append(TimelineEvent(at=asset.created_at, before=None, after=initial_status,
                                      analyst_id=None, analyst_name=None, is_entry=True))
    for log, user in logs:
        transition = status_change(log)
        if not transition:
            continue
        analyst_id, analyst_name, inferred = analyst_identity(log, user)
        timeline.append(TimelineEvent(
            at=log.created_at, before=transition[0], after=transition[1],
            analyst_id=analyst_id, analyst_name=analyst_name, analyst_role_inferred=inferred,
        ))
    if deletion:
        deleting_user = next((user for log, user in logs if log.id == deletion.id), None)
        analyst_id, analyst_name, inferred = analyst_identity(deletion, deleting_user)
        timeline.append(TimelineEvent(at=deletion.created_at, before=asset.status, after=None,
                                      analyst_id=analyst_id, analyst_name=analyst_name,
                                      analyst_role_inferred=inferred, is_deletion=True))
    timeline.sort(key=lambda item: item.at)
    analysis = next((event.at for event in timeline if event.before == MediaStatus.new_process.value
                     and event.after == MediaStatus.analysis.value), None)
    approval = next((event.at for event in timeline if event.after == MediaStatus.approved.value), None)
    current_start = timeline[-1].at if timeline and timeline[-1].after == asset.status else None
    reason = (deletion.changes or {}).get("reason") if deletion else None
    return TrackedProcess(
        asset_id=asset.id, process_code=asset.process_code, official_process_code=asset.official_process_code,
        current_status=asset.status, created_at=asset.created_at,
        deleted_at=deletion.created_at if deletion else None,
        deletion_reason=reason if isinstance(reason, str) else None,
        analysis_started_at=analysis, first_approved_at=approval,
        seconds_to_analysis=elapsed(asset.created_at, analysis),
        seconds_analysis_to_approval=elapsed(analysis, approval) if analysis and approval and approval >= analysis else None,
        seconds_to_approval=elapsed(asset.created_at, approval),
        seconds_in_current_status=elapsed(current_start, now) if not deletion else None,
        timeline=timeline,
    )


def period_filters(column, from_date: date | None, to_date: date | None):
    if from_date and to_date and from_date > to_date:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="A data inicial deve ser anterior ou igual à data final.")
    filters = []
    if from_date:
        filters.append(column >= datetime.combine(from_date, time.min, BUSINESS_TIMEZONE).astimezone(UTC))
    if to_date and to_date < date.max:
        filters.append(column < datetime.combine(to_date + timedelta(days=1), time.min,
                                                 BUSINESS_TIMEZONE).astimezone(UTC))
    return filters


@router.get("/analysts/summary", response_model=list[AnalystActivitySummary])
async def analyst_activity_summary(
    from_date: date | None = None,
    to_date: date | None = None,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_roles("admin")),
) -> list[AnalystActivitySummary]:
    actor = aliased(User)
    status_after = func.coalesce(
        ActivityLog.changes["status"]["after"].astext,
        ActivityLog.changes["asset"]["status"]["after"].astext,
    )
    status_before = func.coalesce(
        ActivityLog.changes["status"]["before"].astext,
        ActivityLog.changes["asset"]["status"]["before"].astext,
    )
    rows = await session.execute(
        select(
            ActivityLog.actor_user_id,
            func.max(func.coalesce(ActivityLog.actor_name, actor.full_name)),
            func.count(func.distinct(ActivityLog.process_code)),
            func.count(),
            func.count().filter(and_(status_before == MediaStatus.new_process.value,
                                     status_after == MediaStatus.analysis.value)),
            func.count().filter(ActivityLog.activity_type == "aprovacao"),
        )
        .outerjoin(actor, ActivityLog.actor_user_id == actor.id)
        .where(ActivityLog.actor_user_id.is_not(None),
               or_(ActivityLog.actor_role == "analyst", and_(ActivityLog.actor_role.is_(None), actor.role == "analyst")),
               *period_filters(ActivityLog.created_at, from_date, to_date))
        .group_by(ActivityLog.actor_user_id)
    )
    summaries = {
        user_id: AnalystActivitySummary(id=user_id, name=name or "Analista", processes_touched=processes,
                                        actions=actions, analyses_started=starts, approvals=approvals)
        for user_id, name, processes, actions, starts, approvals in rows
    }
    current = await session.scalars(select(User).where(User.role == "analyst").order_by(User.full_name))
    for user in current:
        summaries.setdefault(user.id, AnalystActivitySummary(
            id=user.id, name=user.full_name, processes_touched=0, actions=0, analyses_started=0, approvals=0,
        ))
    return sorted(summaries.values(), key=lambda item: item.name)


@router.get("/deleted", response_model=TrackingPage)
async def list_deleted_process_tracking(
    search: str | None = Query(default=None, max_length=120),
    from_date: date | None = None,
    to_date: date | None = None,
    analyst_id: UUID | None = None,
    current_status: MediaStatus | None = None,
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_roles("admin")),
) -> TrackingPage:
    filters = [ActivityLog.activity_type == "remocao", *period_filters(ActivityLog.created_at, from_date, to_date)]
    if search and (term := search.strip()):
        pattern = f"%{term}%"
        filters.append(or_(ActivityLog.process_code.ilike(pattern),
                           ActivityLog.changes["before"]["official_process_code"].astext.ilike(pattern)))
    if current_status:
        filters.append(ActivityLog.changes["before"]["status"].astext == current_status.value)
    if analyst_id:
        previous = aliased(ActivityLog)
        actor = aliased(User)
        filters.append(select(previous.id).outerjoin(actor, previous.actor_user_id == actor.id).where(
            previous.process_code == ActivityLog.process_code,
            previous.actor_user_id == analyst_id,
            or_(previous.actor_role == "analyst", and_(previous.actor_role.is_(None), actor.role == "analyst")),
        ).exists())

    total = await session.scalar(select(func.count()).select_from(ActivityLog).where(*filters)) or 0
    removals = list(await session.scalars(select(ActivityLog).where(*filters)
                                        .order_by(ActivityLog.created_at.desc(), ActivityLog.id.desc())
                                        .offset(offset).limit(limit)))
    if not removals:
        return TrackingPage(items=[], total=total, limit=limit, offset=offset)

    codes = [log.process_code for log in removals]
    actor = aliased(User)
    rows = await session.execute(select(ActivityLog, actor).outerjoin(actor, ActivityLog.actor_user_id == actor.id)
                                 .where(ActivityLog.process_code.in_(codes))
                                 .order_by(ActivityLog.created_at, ActivityLog.id))
    grouped: dict[str, list[tuple[ActivityLog, User | None]]] = {code: [] for code in codes}
    for log, user in rows:
        grouped[log.process_code].append((log, user))

    items = []
    for removal in removals:
        changes = removal.changes or {}
        before = changes.get("before") if isinstance(changes.get("before"), dict) else {}
        registration = next((log for log, _ in grouped[removal.process_code] if log.activity_type == "cadastro"), None)
        recorded_creation = changes.get("created_at")
        try:
            created = datetime.fromisoformat(recorded_creation) if isinstance(recorded_creation, str) else None
        except ValueError:
            created = None
        if created is None and registration:
            created = registration.created_at
        archived = SimpleNamespace(
            id=None, process_code=removal.process_code, official_process_code=before.get("official_process_code"),
            status=before.get("status"), created_at=created,
        )
        items.append(tracked_process(archived, grouped[removal.process_code], removal.created_at, removal))
    return TrackingPage(items=items, total=total, limit=limit, offset=offset)


@router.get("/analysts", response_model=list[TrackedAnalyst])
async def list_tracked_analysts(
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_roles("admin")),
) -> list[TrackedAnalyst]:
    current_analysts = list(await session.scalars(select(User).where(User.role == "analyst").order_by(User.full_name)))
    options = {user.id: user.full_name for user in current_analysts}
    actor = aliased(User)
    rows = await session.execute(
        select(ActivityLog.actor_user_id, func.max(func.coalesce(ActivityLog.actor_name, actor.full_name)))
        .outerjoin(actor, ActivityLog.actor_user_id == actor.id)
        .where(ActivityLog.actor_user_id.is_not(None),
               or_(ActivityLog.actor_role == "analyst", and_(ActivityLog.actor_role.is_(None), actor.role == "analyst")))
        .group_by(ActivityLog.actor_user_id)
        .order_by(func.max(func.coalesce(ActivityLog.actor_name, actor.full_name)))
    )
    for user_id, name in rows:
        options.setdefault(user_id, name or "Analista")
    return [TrackedAnalyst(id=user_id, name=name) for user_id, name in sorted(options.items(), key=lambda item: item[1])]


@router.get("", response_model=TrackingPage)
async def list_process_tracking(
    search: str | None = Query(default=None, max_length=120),
    from_date: date | None = None,
    to_date: date | None = None,
    analyst_id: UUID | None = None,
    current_status: MediaStatus | None = None,
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_roles("admin")),
) -> TrackingPage:
    filters = period_filters(MediaAsset.created_at, from_date, to_date)
    if search and (term := search.strip()):
        pattern = f"%{term}%"
        filters.append(or_(MediaAsset.process_code.ilike(pattern), MediaAsset.official_process_code.ilike(pattern)))
    if current_status:
        filters.append(MediaAsset.status == current_status.value)
    if analyst_id:
        actor = aliased(User)
        filters.append(select(ActivityLog.id).outerjoin(actor, ActivityLog.actor_user_id == actor.id).where(
            ActivityLog.asset_id == MediaAsset.id,
            ActivityLog.actor_user_id == analyst_id,
            or_(ActivityLog.actor_role == "analyst", and_(ActivityLog.actor_role.is_(None), actor.role == "analyst")),
        ).exists())

    total = await session.scalar(select(func.count()).select_from(MediaAsset).where(*filters)) or 0
    assets = list(await session.scalars(select(MediaAsset).where(*filters)
                                       .order_by(MediaAsset.created_at.desc(), MediaAsset.id.desc())
                                       .offset(offset).limit(limit)))
    if not assets:
        return TrackingPage(items=[], total=total, limit=limit, offset=offset)

    ids = [asset.id for asset in assets]
    actor = aliased(User)
    rows = await session.execute(select(ActivityLog, actor).outerjoin(actor, ActivityLog.actor_user_id == actor.id)
                                 .where(ActivityLog.asset_id.in_(ids))
                                 .order_by(ActivityLog.created_at, ActivityLog.id))
    grouped: dict[UUID, list[tuple[ActivityLog, User | None]]] = {asset_id: [] for asset_id in ids}
    for log, user in rows:
        grouped[log.asset_id].append((log, user))
    now = datetime.now(UTC)
    return TrackingPage(items=[tracked_process(asset, grouped[asset.id], now) for asset in assets],
                        total=total, limit=limit, offset=offset)
