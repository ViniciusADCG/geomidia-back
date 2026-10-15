from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import contains_eager, selectinload

from app.api.routes.media_assets import (
    APPROVAL_LOCK_ID,
    analyze_asset,
    approval_conflict_confirmation_detail,
    asset_for_user,
    asset_snapshot,
    conflict_snapshots_match,
    ensure_direct_status_change_allowed,
    log_activity,
)
from app.core.security import require_roles
from app.db.models import ActivityLog, ApplicationForm, ApplicationFormAttachment, MediaAsset, User
from app.db.session import get_session
from app.schemas import (
    ActivityType,
    ApplicationFormBase,
    ApplicationFormPage,
    ApplicationFormRead,
    ApplicationFormUpdate,
    AttachmentDownloadRead,
    LinkedApplicationFormRead,
    LinkedApplicationFormUpdate,
    MediaAssetBase,
    MediaStatus,
)
from app.services.storage import StorageConfigurationError, StorageRequestError, SupabaseStorage

router = APIRouter(prefix="/application-forms", tags=["application-forms"])
FORM_ROLES = ("admin", "analyst")


def asset_data_from_form(form: ApplicationFormBase) -> dict[str, Any]:
    return {
        "media_type": form.media_type.value,
        "address": f"{form.street}, {form.number}",
        "district": form.district,
        "latitude": form.latitude,
        "longitude": form.longitude,
        "area_m2": form.area_m2,
        "area_rule_classification": form.area_rule_classification.value if form.area_rule_classification else None,
        "bottom_height_m": form.bottom_height_m,
        "expiration_date": form.expiration_date,
        "attachment_links": form.attachment_links,
        "contact_name": form.company_responsible,
        "contact_email": str(form.requester_email),
    }


async def get_form_or_404(form_id: UUID, session: AsyncSession) -> ApplicationForm:
    application_form = await session.get(ApplicationForm, form_id)
    if application_form is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Formulario nao encontrado.")
    return application_form


@router.patch("/{form_id}/linked", response_model=LinkedApplicationFormRead)
async def update_linked_application_form(
    form_id: UUID,
    payload: LinkedApplicationFormUpdate,
    request: Request,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_roles(*FORM_ROLES)),
) -> LinkedApplicationFormRead:
    try:
        application_form = await get_form_or_404(form_id, session)
        asset = await session.get(MediaAsset, application_form.asset_id)
        linked_asset = application_form.asset
        if asset is None or linked_asset is None or application_form.asset_id != asset.id or linked_asset.id != asset.id:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Vinculo entre formulario e ativo invalido.")

        form_changes = payload.form.model_dump(exclude_unset=True, mode="json")
        asset_changes = payload.asset.model_dump(exclude_unset=True, mode="json")
        confirm_conflict_approval = asset_changes.pop("confirm_conflict_approval", False)
        confirmed_conflicts = asset_changes.pop("confirmed_conflicts", None)
        required_form = {
            name for name, info in ApplicationFormBase.model_fields.items() if info.is_required()
        }
        required_asset = {"media_type", "address", "district", "latitude", "longitude", "status"}
        if any(form_changes.get(name, object()) is None for name in required_form):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Campo obrigatorio nulo.")
        if any(asset_changes.get(name, object()) is None for name in required_asset):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Campo obrigatorio nulo.")

        before_form = ApplicationFormBase.model_validate(application_form).model_dump(mode="json")
        before_asset = asset_snapshot(asset)
        try:
            validated_form = ApplicationFormBase.model_validate({**before_form, **form_changes})
            form_values = validated_form.model_dump(mode="json")
            shared_fields = {"media_type", "district", "latitude", "longitude", "expiration_date"}
            for field in shared_fields.intersection(form_changes):
                asset_changes[field] = form_values[field]
            if {"street", "number"}.intersection(form_changes):
                asset_changes["address"] = f"{validated_form.street}, {validated_form.number}"
            if "requester_email" in form_changes:
                asset_changes["contact_email"] = str(validated_form.requester_email)

            ensure_direct_status_change_allowed(asset.status, asset_changes.get("status"))
            merged_asset = {name: before_asset[name] for name in MediaAssetBase.model_fields}
            merged_asset.update({key: value for key, value in asset_changes.items() if key != "official_process_code"})
            validated_asset = MediaAssetBase.model_validate(merged_asset)
        except ValidationError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

        if validated_asset.status == MediaStatus.irregular and not (validated_asset.justification or "").strip():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="A justificativa e obrigatoria para marcar um processo como irregular.",
            )

        if "official_process_code" in asset_changes:
            official_code = (asset_changes.pop("official_process_code") or "").strip() or None
        else:
            official_code = asset.official_process_code
        for field in form_changes:
            if field != "expiration_date":
                setattr(application_form, field, form_values[field])
        validated_asset_values = validated_asset.model_dump(mode="json")
        for field, value in asset_changes.items():
            if field == "expiration_date":
                value = validated_asset.expiration_date
            else:
                value = validated_asset_values.get(field, value)
            setattr(asset, field, value)
        asset.official_process_code = official_code

        spatial_fields = {"media_type", "latitude", "longitude", "area_m2", "area_rule_classification"}
        spatial_asset_changed = any(
            getattr(asset, field) != before_asset[field]
            for field in spatial_fields
        )
        requires_approval_review = (
            asset.status == MediaStatus.approved.value
            and (before_asset["status"] != MediaStatus.approved.value or spatial_asset_changed)
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

        after_asset = asset_snapshot(asset)
        after_form = ApplicationFormBase.model_validate(application_form).model_dump(mode="json")
        asset_diff = {
            field: {"before": before_asset.get(field), "after": value}
            for field, value in after_asset.items() if before_asset.get(field) != value
        }
        form_diff = {
            field: {"before": before_form.get(field), "after": value}
            for field, value in after_form.items() if before_form.get(field) != value
        }
        if asset_diff or form_diff:
            if before_asset["status"] != asset.status and asset.status == MediaStatus.approved.value:
                activity_type = ActivityType.aprovacao
                message = f"Processo {asset.process_code} aprovado. {asset.justification or ''}".strip()
            elif before_asset["status"] != asset.status and asset.status == MediaStatus.irregular.value:
                activity_type = ActivityType.reprovacao
                message = f"Processo {asset.process_code} marcado como irregular. {asset.justification or ''}".strip()
            else:
                activity_type = ActivityType.edicao
                message = f"Formulario e processo {asset.process_code} atualizados."
            session.add(log_activity(
                asset, activity_type, message,
                current_user, request.state.request_id,
                {
                    "asset": asset_diff,
                    "form": form_diff,
                    "form_id": str(application_form.id),
                    **({"conflict_approval": {"confirmed": True, "conflicts": approved_conflicts}} if approved_conflicts else {}),
                },
            ))

        await session.flush()
        result = LinkedApplicationFormRead(
            form=ApplicationFormRead.model_validate(application_form),
            asset=asset_for_user(asset, current_user).model_copy(update={
                "company_responsible": application_form.company_responsible,
                "company_cnpj": application_form.company_cnpj,
            }),
        )
        await session.commit()
        return result
    except Exception:
        await session.rollback()
        raise


@router.get("", response_model=ApplicationFormPage)
async def list_application_forms(
    search: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_roles(*FORM_ROLES)),
) -> ApplicationFormPage:
    filters = []
    if search and search.strip():
        pattern = f"%{search.strip()}%"
        filters.append(
            or_(
                MediaAsset.process_code.ilike(pattern),
                ApplicationForm.company_responsible.ilike(pattern),
                ApplicationForm.company_cnpj.ilike(pattern),
                ApplicationForm.municipal_registration.ilike(pattern),
                ApplicationForm.property_registration.ilike(pattern),
                ApplicationForm.street.ilike(pattern),
                ApplicationForm.district.ilike(pattern),
            )
        )
    base = select(ApplicationForm).join(MediaAsset, ApplicationForm.asset_id == MediaAsset.id).where(*filters)
    total = await session.scalar(
        select(func.count(ApplicationForm.id)).join(MediaAsset, ApplicationForm.asset_id == MediaAsset.id).where(*filters)
    )
    result = await session.scalars(
        base.options(
            contains_eager(ApplicationForm.asset).load_only(
                MediaAsset.id, MediaAsset.process_code, MediaAsset.status, MediaAsset.expiration_date,
            ),
            selectinload(ApplicationForm.attachments),
        )
        .order_by(ApplicationForm.created_at.desc(), ApplicationForm.id.desc())
        .limit(limit).offset(offset)
    )
    return ApplicationFormPage(items=list(result), total=total or 0, limit=limit, offset=offset)


@router.get("/by-asset/{asset_id}", response_model=ApplicationFormRead)
async def get_application_form_for_asset(
    asset_id: UUID,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_roles(*FORM_ROLES)),
) -> ApplicationForm:
    application_form = await session.scalar(
        select(ApplicationForm)
        .options(selectinload(ApplicationForm.attachments))
        .where(ApplicationForm.asset_id == asset_id)
    )
    if application_form is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Formulario nao encontrado.")
    return application_form


@router.get("/{form_id}", response_model=ApplicationFormRead)
async def get_application_form(
    form_id: UUID,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_roles(*FORM_ROLES)),
) -> ApplicationForm:
    return await get_form_or_404(form_id, session)


@router.patch("/{form_id}", response_model=ApplicationFormRead)
async def update_application_form(
    form_id: UUID,
    payload: ApplicationFormUpdate,
    request: Request,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_roles(*FORM_ROLES)),
) -> ApplicationForm:
    application_form = await get_form_or_404(form_id, session)
    changes = payload.model_dump(exclude_unset=True, mode="json")
    required_fields = {
        field_name for field_name, field_info in ApplicationFormBase.model_fields.items() if field_info.is_required()
    }
    invalid_nulls = required_fields.intersection(field for field, value in changes.items() if value is None)
    if invalid_nulls:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Campos obrigatorios nao podem ser nulos: {', '.join(sorted(invalid_nulls))}.",
        )

    current = ApplicationFormBase.model_validate(application_form).model_dump(mode="json")
    validated = ApplicationFormBase.model_validate({**current, **changes})
    form_values = validated.model_dump(mode="json")
    before = asset_snapshot(application_form.asset)

    for field, value in form_values.items():
        if field == "expiration_date":
            continue
        setattr(application_form, field, value)

    asset_values = asset_data_from_form(validated)
    for field, value in asset_values.items():
        setattr(application_form.asset, field, value)
    after = asset_snapshot(application_form.asset)
    changed = {
        field: {"before": before.get(field), "after": value}
        for field, value in after.items()
        if before.get(field) != value
    }
    session.add(
        log_activity(
            application_form.asset,
            ActivityType.edicao,
            f"Formulario do processo {application_form.asset.process_code} atualizado.",
            current_user,
            request.state.request_id,
            {"asset": changed, "form_id": str(application_form.id)},
        )
    )
    await session.commit()
    await session.refresh(application_form)
    return application_form


@router.get("/{form_id}/attachments/{attachment_id}/download", response_model=AttachmentDownloadRead)
async def get_application_form_attachment_download(
    form_id: UUID,
    attachment_id: UUID,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_roles(*FORM_ROLES)),
) -> AttachmentDownloadRead:
    attachment = await session.scalar(
        select(ApplicationFormAttachment).where(
            ApplicationFormAttachment.id == attachment_id,
            ApplicationFormAttachment.application_form_id == form_id,
        )
    )
    if attachment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Anexo nao encontrado.")
    try:
        url = await SupabaseStorage().create_download_url(attachment.object_path)
    except (StorageConfigurationError, StorageRequestError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Nao foi possivel liberar o download do anexo.",
        ) from exc
    return AttachmentDownloadRead(url=url)


@router.delete("/{form_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_application_form(
    form_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_roles("admin")),
) -> None:
    application_form = await get_form_or_404(form_id, session)
    asset = application_form.asset
    session.add(
        ActivityLog(
            asset_id=None,
            actor_user_id=current_user.id,
            process_code=asset.process_code,
            activity_type=ActivityType.remocao.value,
            message=f"Formulario e processo {asset.process_code} removidos do sistema.",
            request_id=request.state.request_id,
            changes={"before": asset_snapshot(asset), "form_id": str(application_form.id), "after": None},
        )
    )
    await session.delete(asset)
    await session.commit()
