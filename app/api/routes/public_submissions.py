import asyncio
import hmac
import logging
import re
import secrets
import unicodedata
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.application_forms import asset_data_from_form
from app.api.routes.media_assets import asset_snapshot, log_activity
from app.api.routes.media_rules import active_rule_for_type, calculate_rule_radius
from app.core.config import Settings, get_settings
from app.db.models import (
    ApplicationForm,
    ApplicationFormAttachment,
    MediaAsset,
    MediaRule,
    PublicProcessCounter,
    PublicSubmissionDraft,
)
from app.db.session import get_session
from app.schemas import (
    ActivityType,
    ApplicationFormBase,
    MediaStatus,
    PublicAttachmentInput,
    PublicNewProcessPayload,
    PublicSubmissionFinalize,
    PublicSubmissionInitiate,
    PublicSubmissionInitiated,
    PublicSubmissionResult,
    PublicUploadTarget,
)
from app.services.protocols import format_protocol, protocol_year
from app.services.receipt import ReceiptAttachment, ReceiptData, generate_receipt_pdf, send_receipt_email
from app.services.storage import StorageConfigurationError, StorageRequestError, SupabaseStorage

router = APIRouter(prefix="/public/solicitacoes/veiculos-divulgacao", tags=["public-submissions"])
logger = logging.getLogger(__name__)

UPLOAD_RULES = {
    "alvaraLocalizacao": {"min": 1, "max": 5, "types": {"pdf", "image"}},
    "requerimentoPadrao": {"min": 1, "max": 1, "types": {"pdf"}},
    "autorizacaoProprietario": {"min": 1, "max": 5, "types": {"pdf", "image"}},
    "documentoProprietario": {"min": 0, "max": 5, "types": {"pdf", "image"}},
    "projetoEstrutural": {"min": 1, "max": 5, "types": {"pdf"}},
    "projetoImplantacao": {"min": 1, "max": 5, "types": {"pdf"}},
    "artRrt": {"min": 1, "max": 5, "types": {"pdf"}},
}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".heif"}
DRAFT_LIFETIME = timedelta(hours=2)
RATE_LIMIT_WINDOW = timedelta(minutes=10)
MINIMUM_FILL_TIME = timedelta(seconds=3)


def validate_public_origin(request: Request, settings: Settings) -> None:
    origin = (request.headers.get("origin") or "").rstrip("/")
    if not origin or origin not in settings.public_form_origin_list:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Origem do formulario nao autorizada.")


def client_fingerprint(request: Request, settings: Settings) -> str:
    forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
    address = forwarded or (request.client.host if request.client else "unknown")
    return hmac.new(settings.jwt_secret.encode(), address.encode(), sha256).hexdigest()


def safe_filename(filename: str) -> str:
    normalized = unicodedata.normalize("NFKD", filename)
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", ascii_name).strip(".-")
    cleaned = re.sub(r"-+(\.[A-Za-z0-9]+)$", r"\1", cleaned)
    return (cleaned or "arquivo")[-120:]


def attachment_kind(attachment: PublicAttachmentInput) -> str | None:
    extension = f".{attachment.filename.rsplit('.', 1)[-1].lower()}" if "." in attachment.filename else ""
    if attachment.content_type == "application/pdf" and extension == ".pdf":
        return "pdf"
    if attachment.content_type.startswith("image/") and extension in IMAGE_EXTENSIONS:
        return "image"
    return None


def validate_attachment_manifest(attachments: list[PublicAttachmentInput]) -> None:
    counts = Counter(item.category for item in attachments)
    if len({item.client_id for item in attachments}) != len(attachments):
        raise HTTPException(status_code=422, detail="Identificadores de anexos duplicados.")

    unknown_categories = set(counts).difference(UPLOAD_RULES)
    if unknown_categories:
        raise HTTPException(status_code=422, detail="Categoria de anexo desconhecida.")

    for category, rule in UPLOAD_RULES.items():
        count = counts.get(category, 0)
        if count < rule["min"] or count > rule["max"]:
            raise HTTPException(status_code=422, detail=f"Quantidade invalida de anexos em {category}.")

    for attachment in attachments:
        kind = attachment_kind(attachment)
        if kind not in UPLOAD_RULES[attachment.category]["types"]:
            raise HTTPException(status_code=422, detail=f"Formato invalido para {attachment.filename}.")


def validate_submission_timing(payload: PublicNewProcessPayload) -> None:
    if payload.website.strip():
        raise HTTPException(status_code=422, detail="Solicitacao invalida.")
    if not payload.acknowledgement:
        raise HTTPException(status_code=422, detail="A confirmacao de ciencia e obrigatoria.")
    if payload.started_at.tzinfo is None:
        raise HTTPException(status_code=422, detail="Horario inicial invalido.")
    elapsed = datetime.now(UTC) - payload.started_at.astimezone(UTC)
    if elapsed < MINIMUM_FILL_TIME or elapsed > DRAFT_LIFETIME:
        raise HTTPException(status_code=422, detail="Tempo de preenchimento invalido. Recarregue o formulario.")


def application_form_from_public(payload: PublicNewProcessPayload) -> ApplicationFormBase:
    return ApplicationFormBase.model_validate(
        {
            "company_responsible": payload.applicant.company,
            "company_cnpj": payload.applicant.company_cnpj,
            "municipal_registration": payload.applicant.municipal_registration,
            "property_registration": payload.location.property_registration,
            "latitude": payload.location.latitude,
            "longitude": payload.location.longitude,
            "street": payload.location.street,
            "number": payload.location.number,
            "district": payload.location.district,
            "postal_code": payload.location.postal_code,
            "media_type": payload.vehicle.media_type,
            "area_m2": payload.vehicle.area_m2,
            "area_rule_classification": payload.vehicle.area_rule_classification,
            "bottom_height_m": payload.vehicle.bottom_height_m,
            "number_of_faces": payload.vehicle.number_of_faces,
            "requester_email": payload.email,
            "attachment_links": None,
        }
    )


def validate_public_vehicle_rule(rule: MediaRule, payload: PublicNewProcessPayload) -> None:
    classification = payload.vehicle.area_rule_classification
    if rule.area_threshold_m2 is None and classification is not None:
        raise HTTPException(status_code=422, detail="Este tipo de veiculo nao usa classificacao de area.")
    calculate_rule_radius(rule, payload.vehicle.area_m2, classification)


@router.get("/regras", response_model=list[dict])
async def public_vehicle_rules(session: AsyncSession = Depends(get_session)) -> list[dict]:
    rules = await session.scalars(select(MediaRule).where(MediaRule.is_active.is_(True)))
    return [
        {"tipo": rule.media_type, "limiteAreaM2": rule.area_threshold_m2}
        for rule in rules
    ]


def storage_or_503(settings: Settings) -> SupabaseStorage:
    try:
        return SupabaseStorage(settings)
    except StorageConfigurationError as exc:
        raise HTTPException(status_code=503, detail="Recebimento de anexos temporariamente indisponivel.") from exc


async def next_public_process_code(session: AsyncSession, year: int) -> str:
    """Allocate a VEI number independently from internal PROC numbers."""
    existing_codes = await session.scalars(
        select(MediaAsset.process_code).where(MediaAsset.process_code.like(f"VEI-%-{year}"))
    )
    existing_numbers = []
    for code in existing_codes:
        try:
            existing_numbers.append(int(code.split("-")[1]))
        except (IndexError, ValueError):
            continue
    first_value = max(existing_numbers, default=0) + 1
    number = await session.scalar(
        pg_insert(PublicProcessCounter)
        .values(year=year, last_value=first_value)
        .on_conflict_do_update(
            index_elements=[PublicProcessCounter.year],
            set_={"last_value": func.greatest(PublicProcessCounter.last_value, first_value - 1) + 1},
        )
        .returning(PublicProcessCounter.last_value)
    )
    return format_protocol("VEI", number, year)


async def receipt_data_for_draft(draft: PublicSubmissionDraft, session: AsyncSession) -> ReceiptData | None:
    form = await session.scalar(
        select(ApplicationForm)
        .join(MediaAsset, ApplicationForm.asset_id == MediaAsset.id)
        .where(MediaAsset.process_code == draft.process_code)
    )
    if form is None:
        return None
    return ReceiptData(
        process_code=draft.process_code or "",
        finalized_at=draft.finalized_at or datetime.now(UTC),
        requester_email=form.requester_email,
        company=form.company_responsible,
        company_cnpj=form.company_cnpj,
        municipal_registration=form.municipal_registration,
        property_registration=form.property_registration,
        latitude=form.latitude,
        longitude=form.longitude,
        street=form.street,
        number=form.number,
        district=form.district,
        postal_code=form.postal_code,
        media_type=form.media_type,
        attachments=tuple(
            ReceiptAttachment(item.category, item.original_filename)
            for item in sorted(form.attachments, key=lambda item: (item.category, item.original_filename))
        ),
    )


async def send_finalized_receipt(draft: PublicSubmissionDraft, session: AsyncSession, settings: Settings) -> bool:
    """Send once per draft when SMTP is available; allow a later finalize retry after failure."""
    if draft.receipt_sent_at:
        return True
    if not settings.receipt_email_configured:
        return False

    receipt = await receipt_data_for_draft(draft, session)
    if receipt is None:
        logger.error("Application form missing for finalized process %s", draft.process_code)
        return False
    try:
        pdf = generate_receipt_pdf(receipt)
        await asyncio.to_thread(send_receipt_email, receipt, pdf, settings)
    except Exception:
        logger.exception("Failed to send receipt for process %s", draft.process_code)
        return False

    draft.receipt_sent_at = datetime.now(UTC)
    await session.commit()
    return True


def result_for_receipt(process_code: str) -> PublicSubmissionResult:
    return PublicSubmissionResult(
        protocolo=process_code,
        message="Solicitação recebida. Baixe o comprovante PDF abaixo e guarde o protocolo.",
        receipt_sent=False,
    )


@router.post("/iniciar", response_model=PublicSubmissionInitiated, status_code=status.HTTP_201_CREATED)
async def initiate_public_submission(
    body: PublicSubmissionInitiate,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> PublicSubmissionInitiated:
    settings = get_settings()
    validate_public_origin(request, settings)
    validate_submission_timing(body.payload)
    validate_attachment_manifest(body.attachments)
    rule = await active_rule_for_type(body.payload.vehicle.media_type.value, session)
    validate_public_vehicle_rule(rule, body.payload)

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
    attachment_records = []
    for attachment in body.attachments:
        attachment_records.append(
            {
                **attachment.model_dump(),
                "object_path": (
                    f"public-submissions/{draft_id}/{attachment.category}/"
                    f"{uuid.uuid4()}-{safe_filename(attachment.filename)}"
                ),
            }
        )

    try:
        signed_uploads = await asyncio.gather(
            *(storage.create_signed_upload(item["object_path"]) for item in attachment_records)
        )
    except StorageRequestError as exc:
        raise HTTPException(status_code=503, detail="Nao foi possivel preparar o envio dos anexos.") from exc

    draft = PublicSubmissionDraft(
        id=draft_id,
        token_hash=sha256(token.encode()).hexdigest(),
        client_fingerprint=fingerprint,
        payload=body.payload.model_dump(mode="json"),
        attachments=attachment_records,
    )
    session.add(draft)
    await session.commit()

    return PublicSubmissionInitiated(
        draft_id=draft_id,
        token=token,
        uploads=[
            PublicUploadTarget(
                client_id=record["client_id"],
                object_path=record["object_path"],
                signed_url=signed.signed_url,
            )
            for record, signed in zip(attachment_records, signed_uploads, strict=True)
        ],
    )


@router.post("/{draft_id}/finalizar", response_model=PublicSubmissionResult)
async def finalize_public_submission(
    draft_id: uuid.UUID,
    body: PublicSubmissionFinalize,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> PublicSubmissionResult:
    settings = get_settings()
    validate_public_origin(request, settings)
    draft = await session.scalar(
        select(PublicSubmissionDraft).where(PublicSubmissionDraft.id == draft_id).with_for_update()
    )
    if draft is None or not hmac.compare_digest(draft.token_hash, sha256(body.token.encode()).hexdigest()):
        raise HTTPException(status_code=404, detail="Solicitacao nao encontrada.")
    if draft.finalized_at:
        if (draft.process_code or "").startswith("HESP-"):
            raise HTTPException(status_code=404, detail="Solicitacao nao encontrada.")
        return result_for_receipt(draft.process_code or "")
    if draft.payload.get("process_type") != "PROCESSO_NOVO":
        raise HTTPException(status_code=404, detail="Solicitacao nao encontrada.")
    if datetime.now(UTC) - draft.created_at.astimezone(UTC) > DRAFT_LIFETIME:
        raise HTTPException(status_code=410, detail="O envio expirou. Preencha o formulario novamente.")

    storage = storage_or_503(settings)
    try:
        object_metadata = await asyncio.gather(
            *(storage.get_object_metadata(item["object_path"]) for item in draft.attachments)
        )
    except StorageRequestError as exc:
        raise HTTPException(status_code=503, detail="Nao foi possivel validar os anexos enviados.") from exc

    for attachment, metadata in zip(draft.attachments, object_metadata, strict=True):
        if metadata is None:
            raise HTTPException(status_code=409, detail=f"O anexo {attachment['filename']} nao foi enviado.")
        if metadata.size_bytes is not None and metadata.size_bytes != attachment["size_bytes"]:
            raise HTTPException(status_code=409, detail=f"O tamanho do anexo {attachment['filename']} diverge do informado.")

    public_payload = PublicNewProcessPayload.model_validate(draft.payload)
    validated = application_form_from_public(public_payload)
    rule = await active_rule_for_type(validated.media_type.value, session)
    validate_public_vehicle_rule(rule, public_payload)
    finalized_at = datetime.now(UTC)
    asset = MediaAsset(
        **asset_data_from_form(validated),
        process_code=await next_public_process_code(session, protocol_year(finalized_at)),
        radius_meters=calculate_rule_radius(rule, validated.area_m2, validated.area_rule_classification),
        status=MediaStatus.new_process.value,
    )
    session.add(asset)
    await session.flush()

    form_values = validated.model_dump(mode="json", exclude={"expiration_date"})
    application_form = ApplicationForm(**form_values, asset_id=asset.id, asset=asset)
    session.add(application_form)
    await session.flush()

    for attachment in draft.attachments:
        session.add(
            ApplicationFormAttachment(
                application_form_id=application_form.id,
                category=attachment["category"],
                object_path=attachment["object_path"],
                original_filename=attachment["filename"],
                content_type=attachment["content_type"],
                size_bytes=attachment["size_bytes"],
            )
        )

    session.add(
        log_activity(
            asset,
            ActivityType.cadastro,
            f"Formulario publico cadastrado para {validated.company_responsible} e vinculado ao processo {asset.process_code}.",
            request_id=request.state.request_id,
            changes={"after": asset_snapshot(asset), "form_id": str(application_form.id), "source": "public-form"},
        )
    )
    draft.finalized_at = finalized_at
    draft.process_code = asset.process_code
    draft.payload = {}
    draft.attachments = []
    await session.commit()
    return result_for_receipt(asset.process_code)


@router.post("/{draft_id}/comprovante")
async def download_public_receipt(
    draft_id: uuid.UUID,
    body: PublicSubmissionFinalize,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Response:
    validate_public_origin(request, get_settings())
    draft = await session.scalar(select(PublicSubmissionDraft).where(PublicSubmissionDraft.id == draft_id))
    if (
        draft is None
        or not draft.finalized_at
        or (draft.process_code or "").startswith("HESP-")
        or not hmac.compare_digest(draft.token_hash, sha256(body.token.encode()).hexdigest())
    ):
        raise HTTPException(status_code=404, detail="Comprovante não encontrado.")
    receipt = await receipt_data_for_draft(draft, session)
    if receipt is None:
        raise HTTPException(status_code=404, detail="Comprovante não encontrado.")
    return Response(
        content=generate_receipt_pdf(receipt),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{draft.process_code}.pdf"',
            "Cache-Control": "no-store",
        },
    )
