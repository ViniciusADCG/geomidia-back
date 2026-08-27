from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.media_assets import asset_snapshot, log_activity, next_process_code
from app.api.routes.media_rules import active_rule_for_type, calculate_rule_radius
from app.core.security import require_roles
from app.db.models import ActivityLog, ApplicationForm, ApplicationFormAttachment, MediaAsset, User
from app.db.session import get_session
from app.schemas import (
    ActivityType,
    ApplicationFormBase,
    ApplicationFormCreate,
    ApplicationFormRead,
    ApplicationFormUpdate,
    AttachmentDownloadRead,
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


@router.get("", response_model=list[ApplicationFormRead])
async def list_application_forms(
    search: str | None = Query(default=None, max_length=120),
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_roles(*FORM_ROLES)),
) -> list[ApplicationForm]:
    filters = []
    if search:
        pattern = f"%{search.strip()}%"
        filters.append(
            or_(
                ApplicationForm.company_responsible.ilike(pattern),
                ApplicationForm.municipal_registration.ilike(pattern),
                ApplicationForm.property_registration.ilike(pattern),
                ApplicationForm.street.ilike(pattern),
            )
        )
    result = await session.scalars(
        select(ApplicationForm).where(*filters).order_by(ApplicationForm.created_at.desc())
    )
    return list(result)


@router.get("/{form_id}", response_model=ApplicationFormRead)
async def get_application_form(
    form_id: UUID,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_roles(*FORM_ROLES)),
) -> ApplicationForm:
    return await get_form_or_404(form_id, session)


@router.post("", response_model=ApplicationFormRead, status_code=status.HTTP_201_CREATED)
async def create_application_form(
    payload: ApplicationFormCreate,
    request: Request,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_roles(*FORM_ROLES)),
) -> ApplicationForm:
    rule = await active_rule_for_type(payload.media_type.value, session)
    asset_values = asset_data_from_form(payload)
    asset = MediaAsset(
        **asset_values,
        process_code=await next_process_code(session),
        radius_meters=calculate_rule_radius(rule, payload.area_m2),
        status=MediaStatus.new_process.value,
    )
    session.add(asset)
    await session.flush()

    application_form = ApplicationForm(
        **payload.model_dump(mode="json", exclude={"expiration_date"}),
        asset_id=asset.id,
        asset=asset,
    )
    session.add(application_form)
    await session.flush()
    session.add(
        log_activity(
            asset,
            ActivityType.cadastro,
            f"Formulario cadastrado para {payload.company_responsible} e vinculado ao processo {asset.process_code}.",
            current_user,
            request.state.request_id,
            {"after": asset_snapshot(asset), "form_id": str(application_form.id)},
        )
    )
    await session.commit()
    await session.refresh(application_form)
    return application_form


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
    rule = await active_rule_for_type(validated.media_type.value, session)
    application_form.asset.radius_meters = calculate_rule_radius(rule, validated.area_m2)

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
