"""Public submission and HESP receipt for a response to a requirement notice."""

import asyncio
import hmac
import logging
import secrets
import uuid
from datetime import UTC, datetime
from hashlib import sha256

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.public_submissions import (
    DRAFT_LIFETIME,
    MINIMUM_FILL_TIME,
    RATE_LIMIT_WINDOW,
    attachment_kind,
    client_fingerprint,
    safe_filename,
    storage_or_503,
    validate_public_origin,
)
from app.core.config import get_settings
from app.db.models import PublicSubmissionDraft, RequirementResponseCounter
from app.db.session import get_session
from app.schemas import (
    PublicSubmissionFinalize,
    PublicSubmissionInitiated,
    PublicSubmissionResult,
    PublicUploadTarget,
    RequirementResponseInitiate,
    RequirementResponsePayload,
)
from app.services.protocols import format_protocol, protocol_year
from app.services.requirement_receipt import (
    RequirementReceiptData,
    generate_requirement_receipt_pdf,
    send_requirement_receipt_email,
)
from app.services.storage import StorageRequestError

router = APIRouter(prefix="/public/solicitacoes/veiculos-divulgacao/exigencias", tags=["requirement-responses"])
logger = logging.getLogger(__name__)


def validate_requirement_submission(body: RequirementResponseInitiate) -> None:
    payload = body.payload
    if not payload.acknowledgement:
        raise HTTPException(status_code=422, detail="A confirmação de ciência é obrigatória.")
    if payload.website.strip() or payload.started_at.tzinfo is None:
        raise HTTPException(status_code=422, detail="Solicitação inválida.")
    elapsed = datetime.now(UTC) - payload.started_at.astimezone(UTC)
    if elapsed < MINIMUM_FILL_TIME or elapsed > DRAFT_LIFETIME:
        raise HTTPException(status_code=422, detail="Tempo de preenchimento inválido. Recarregue o formulário.")
    if len({item.client_id for item in body.attachments}) != len(body.attachments):
        raise HTTPException(status_code=422, detail="Identificadores de anexos duplicados.")
    for attachment in body.attachments:
        if attachment.category != "respostaExigencia" or attachment_kind(attachment) not in {"pdf", "image"}:
            raise HTTPException(status_code=422, detail=f"Anexo inválido: {attachment.filename}.")


def response_receipt_data(draft: PublicSubmissionDraft) -> RequirementReceiptData:
    payload = RequirementResponsePayload.model_validate(draft.payload)
    return RequirementReceiptData(
        protocol=draft.process_code or "",
        finalized_at=draft.finalized_at or datetime.now(UTC),
        process_number=payload.process_number,
        notice_number=payload.notice_number,
        requester_email=str(payload.email),
        filenames=tuple(item["filename"] for item in draft.attachments),
    )


def result_for_requirement(protocol: str) -> PublicSubmissionResult:
    return PublicSubmissionResult(
        protocolo=protocol,
        message="Resposta recebida. Baixe o comprovante PDF abaixo e guarde o protocolo.",
        receipt_sent=False,
    )


async def send_finalized_requirement_receipt(draft: PublicSubmissionDraft, session: AsyncSession) -> bool:
    if draft.receipt_sent_at:
        return True
    settings = get_settings()
    if not settings.receipt_email_configured:
        return False
    data = response_receipt_data(draft)
    try:
        pdf = generate_requirement_receipt_pdf(data)
        await asyncio.to_thread(send_requirement_receipt_email, data, pdf, settings)
    except Exception:
        logger.exception("Failed to send HESP receipt %s", draft.process_code)
        return False
    draft.receipt_sent_at = datetime.now(UTC)
    await session.commit()
    return True


async def locked_draft(draft_id: uuid.UUID, token: str, session: AsyncSession) -> PublicSubmissionDraft:
    draft = await session.scalar(
        select(PublicSubmissionDraft).where(PublicSubmissionDraft.id == draft_id).with_for_update()
    )
    if (
        draft is None
        or draft.payload.get("process_type") != "RESPOSTA_COMUNICADO_EXIGENCIA"
        or not hmac.compare_digest(draft.token_hash, sha256(token.encode()).hexdigest())
    ):
        raise HTTPException(status_code=404, detail="Resposta não encontrada.")
    return draft


async def next_requirement_response_code(session: AsyncSession, year: int) -> str:
    existing_codes = await session.scalars(
        select(PublicSubmissionDraft.process_code).where(PublicSubmissionDraft.process_code.like(f"HESP-%-{year}"))
    )
    existing_numbers = []
    for code in existing_codes:
        try:
            existing_numbers.append(int(code.split("-")[1]))
        except (IndexError, ValueError):
            continue
    first_value = max(existing_numbers, default=0) + 1
    number = await session.scalar(
        pg_insert(RequirementResponseCounter)
        .values(year=year, last_value=first_value)
        .on_conflict_do_update(
            index_elements=[RequirementResponseCounter.year],
            set_={"last_value": func.greatest(RequirementResponseCounter.last_value, first_value - 1) + 1},
        )
        .returning(RequirementResponseCounter.last_value)
    )
    return format_protocol("HESP", number, year)


@router.post("/iniciar", response_model=PublicSubmissionInitiated, status_code=status.HTTP_201_CREATED)
async def initiate_requirement_response(
    body: RequirementResponseInitiate,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> PublicSubmissionInitiated:
    settings = get_settings()
    validate_public_origin(request, settings)
    validate_requirement_submission(body)
    fingerprint = client_fingerprint(request, settings)
    recent_count = await session.scalar(
        select(func.count(PublicSubmissionDraft.id)).where(
            PublicSubmissionDraft.client_fingerprint == fingerprint,
            PublicSubmissionDraft.created_at >= datetime.now(UTC) - RATE_LIMIT_WINDOW,
        )
    ) or 0
    if recent_count >= settings.public_submission_rate_limit:
        raise HTTPException(status_code=429, detail="Muitas tentativas. Aguarde alguns minutos e tente novamente.")
    storage = storage_or_503(settings)
    draft_id = uuid.uuid4()
    token = secrets.token_urlsafe(32)
    records = [
        {
            **item.model_dump(),
            "object_path": f"public-submissions/{draft_id}/respostaExigencia/{uuid.uuid4()}-{safe_filename(item.filename)}",
        }
        for item in body.attachments
    ]
    try:
        signed_uploads = await asyncio.gather(*(storage.create_signed_upload(item["object_path"]) for item in records))
    except StorageRequestError as exc:
        raise HTTPException(status_code=503, detail="Não foi possível preparar o envio dos anexos.") from exc
    session.add(PublicSubmissionDraft(
        id=draft_id, token_hash=sha256(token.encode()).hexdigest(),
        client_fingerprint=fingerprint, payload=body.payload.model_dump(mode="json"), attachments=records,
    ))
    await session.commit()
    return PublicSubmissionInitiated(
        draft_id=draft_id, token=token,
        uploads=[
            PublicUploadTarget(client_id=record["client_id"], object_path=record["object_path"], signed_url=upload.signed_url)
            for record, upload in zip(records, signed_uploads, strict=True)
        ],
    )


@router.post("/{draft_id}/finalizar", response_model=PublicSubmissionResult)
async def finalize_requirement_response(
    draft_id: uuid.UUID,
    body: PublicSubmissionFinalize,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> PublicSubmissionResult:
    validate_public_origin(request, get_settings())
    draft = await locked_draft(draft_id, body.token, session)
    if draft.finalized_at:
        return result_for_requirement(draft.process_code or "")
    if datetime.now(UTC) - draft.created_at.astimezone(UTC) > DRAFT_LIFETIME:
        raise HTTPException(status_code=410, detail="O envio expirou. Preencha o formulário novamente.")
    storage = storage_or_503(get_settings())
    try:
        metadata = await asyncio.gather(*(storage.get_object_metadata(item["object_path"]) for item in draft.attachments))
    except StorageRequestError as exc:
        raise HTTPException(status_code=503, detail="Não foi possível validar os anexos enviados.") from exc
    for attachment, item in zip(draft.attachments, metadata, strict=True):
        if item is None or (item.size_bytes is not None and item.size_bytes != attachment["size_bytes"]):
            raise HTTPException(status_code=409, detail=f"O anexo {attachment['filename']} não foi enviado corretamente.")
    finalized_at = datetime.now(UTC)
    draft.process_code = await next_requirement_response_code(session, protocol_year(finalized_at))
    draft.finalized_at = finalized_at
    await session.commit()
    return result_for_requirement(draft.process_code)


@router.post("/{draft_id}/comprovante")
async def download_requirement_receipt(
    draft_id: uuid.UUID,
    body: PublicSubmissionFinalize,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Response:
    validate_public_origin(request, get_settings())
    draft = await locked_draft(draft_id, body.token, session)
    if not draft.finalized_at:
        raise HTTPException(status_code=409, detail="A resposta ainda não foi finalizada.")
    pdf = generate_requirement_receipt_pdf(response_receipt_data(draft))
    return Response(
        content=pdf, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{draft.process_code}.pdf"', "Cache-Control": "no-store"},
    )
