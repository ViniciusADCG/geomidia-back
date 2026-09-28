"""Authenticated access to finalized requirement responses and their documents."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.requirement_responses import response_receipt_data
from app.core.security import require_roles
from app.db.models import PublicSubmissionDraft, User
from app.db.session import get_session
from app.schemas import (
    AttachmentDownloadRead,
    RequirementResponseAttachmentRead,
    RequirementResponsePage,
    RequirementResponseRead,
)
from app.services.requirement_receipt import generate_requirement_receipt_pdf
from app.services.storage import StorageConfigurationError, StorageRequestError, SupabaseStorage

router = APIRouter(prefix="/requirement-responses", tags=["requirement-responses-admin"])
RESPONSE_ROLES = ("admin", "analyst")


def finalized_response_filter():
    return (
        PublicSubmissionDraft.finalized_at.is_not(None),
        PublicSubmissionDraft.payload["process_type"].astext == "RESPOSTA_COMUNICADO_EXIGENCIA",
    )


def serialize_response(draft: PublicSubmissionDraft) -> RequirementResponseRead:
    payload = draft.payload
    return RequirementResponseRead(
        id=draft.id,
        protocol=draft.process_code or "",
        process_number=payload["process_number"],
        notice_number=payload["notice_number"],
        requester_email=payload["email"],
        finalized_at=draft.finalized_at,
        receipt_sent=draft.receipt_sent_at is not None,
        attachments=[
            RequirementResponseAttachmentRead(
                index=index,
                filename=item["filename"],
                content_type=item["content_type"],
                size_bytes=item["size_bytes"],
            )
            for index, item in enumerate(draft.attachments)
        ],
    )


async def get_response_or_404(response_id: UUID, session: AsyncSession) -> PublicSubmissionDraft:
    draft = await session.scalar(
        select(PublicSubmissionDraft).where(
            PublicSubmissionDraft.id == response_id,
            *finalized_response_filter(),
        )
    )
    if draft is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resposta não encontrada.")
    return draft


@router.get("", response_model=RequirementResponsePage)
async def list_requirement_responses(
    search: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_roles(*RESPONSE_ROLES)),
) -> RequirementResponsePage:
    filters = list(finalized_response_filter())
    if search and search.strip():
        pattern = f"%{search.strip()}%"
        filters.append(or_(
            PublicSubmissionDraft.process_code.ilike(pattern),
            PublicSubmissionDraft.payload["process_number"].astext.ilike(pattern),
            PublicSubmissionDraft.payload["notice_number"].astext.ilike(pattern),
            PublicSubmissionDraft.payload["email"].astext.ilike(pattern),
        ))
    total = await session.scalar(select(func.count()).select_from(PublicSubmissionDraft).where(*filters)) or 0
    drafts = await session.scalars(
        select(PublicSubmissionDraft)
        .where(*filters)
        .order_by(PublicSubmissionDraft.finalized_at.desc(), PublicSubmissionDraft.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return RequirementResponsePage(
        items=[serialize_response(draft) for draft in drafts],
        total=total, limit=limit, offset=offset,
    )


@router.get("/{response_id}", response_model=RequirementResponseRead)
async def get_requirement_response(
    response_id: UUID,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_roles(*RESPONSE_ROLES)),
) -> RequirementResponseRead:
    return serialize_response(await get_response_or_404(response_id, session))


@router.get("/{response_id}/attachments/{attachment_index}/download", response_model=AttachmentDownloadRead)
async def get_requirement_attachment_download(
    response_id: UUID,
    attachment_index: int,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_roles(*RESPONSE_ROLES)),
) -> AttachmentDownloadRead:
    draft = await get_response_or_404(response_id, session)
    if attachment_index < 0 or attachment_index >= len(draft.attachments):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Anexo não encontrado.")
    try:
        url = await SupabaseStorage().create_download_url(draft.attachments[attachment_index]["object_path"])
    except (StorageConfigurationError, StorageRequestError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Não foi possível liberar o download do anexo.",
        ) from exc
    return AttachmentDownloadRead(url=url)


@router.get("/{response_id}/comprovante")
async def get_requirement_receipt(
    response_id: UUID,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_roles(*RESPONSE_ROLES)),
) -> Response:
    draft = await get_response_or_404(response_id, session)
    pdf = generate_requirement_receipt_pdf(response_receipt_data(draft))
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{draft.process_code}.pdf"',
            "Cache-Control": "no-store",
        },
    )
