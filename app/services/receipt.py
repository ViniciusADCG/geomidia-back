"""Generate and email the receipt for a finalized public submission."""

import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from html import escape
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.core.config import Settings

ASSET_PATH = Path(__file__).resolve().parents[1] / "assets" / "campo_grande.png"
MEDIA_LABELS = {
    "outdoor": "Outdoor",
    "front light": "Painel Iluminado - Front Light",
    "triface": "Painel Iluminado - Triface",
    "empena": "Empena",
    "painel eletronico modular": "Painel Eletrônico Modular",
    "painel de led": "Painel Eletrônico Modular - Pequeno Porte",
    "empena de led": "Empena Eletrônica",
}
CATEGORY_LABELS = {
    "alvaraLocalizacao": "Alvará de localização",
    "requerimentoPadrao": "Requerimento padrão",
    "autorizacaoProprietario": "Autorização do proprietário",
    "documentoProprietario": "Documento do proprietário",
    "projetoEstrutural": "Projeto estrutural",
    "projetoImplantacao": "Projeto de implantação",
    "artRrt": "ART/RRT",
}


@dataclass(frozen=True)
class ReceiptAttachment:
    category: str
    filename: str


@dataclass(frozen=True)
class ReceiptData:
    process_code: str
    finalized_at: datetime
    requester_email: str
    company: str
    company_cnpj: str | None
    municipal_registration: str
    property_registration: str
    latitude: float
    longitude: float
    street: str
    number: str
    district: str
    postal_code: str
    media_type: str
    attachments: tuple[ReceiptAttachment, ...]


def generate_receipt_pdf(data: ReceiptData) -> bytes:
    """Render the canonical receipt from data saved by the backend."""
    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=(21 * cm, 29.7 * cm),
        rightMargin=2 * cm,
        leftMargin=2 * cm,
        topMargin=1.5 * cm,
        bottomMargin=1.5 * cm,
        title=f"Protocolo de Solicitação {data.process_code}",
        author="SEMADES/SURB/GCP",
    )
    styles = getSampleStyleSheet()
    heading = ParagraphStyle(
        "ReceiptHeading", parent=styles["Normal"], alignment=TA_CENTER, fontName="Helvetica-Bold",
        fontSize=9, leading=12,
    )
    title = ParagraphStyle(
        "ReceiptTitle", parent=styles["Heading1"], alignment=TA_CENTER, fontName="Helvetica-Bold",
        fontSize=16, leading=20, spaceAfter=12,
    )
    label = ParagraphStyle("ReceiptLabel", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=9)
    value = ParagraphStyle("ReceiptValue", parent=styles["Normal"], fontSize=9, leading=12)
    note = ParagraphStyle("ReceiptNote", parent=styles["Normal"], fontSize=9, leading=13)

    logo = Image(str(ASSET_PATH), width=1.8 * cm, height=1.7 * cm)
    heading_text = Paragraph(
        "PREFEITURA MUNICIPAL DE CAMPO GRANDE<br/>ESTADO DE MATO GROSSO DO SUL<br/>"
        "SECRETARIA MUNICIPAL DE MEIO AMBIENTE, GESTÃO URBANA E DESENVOLVIMENTO "
        "ECONÔMICO, TURÍSTICO E SUSTENTÁVEL – SEMADES<br/>"
        "SUPERINTENDÊNCIA DE URBANISMO – SURB<br/>GERÊNCIA DE CONTROLE DE POSTURAS – GCP",
        heading,
    )
    header = Table([[logo, heading_text]], colWidths=[2 * cm, 15 * cm])
    header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))

    submitted_at = data.finalized_at.astimezone(ZoneInfo("America/Campo_Grande")).strftime(
        "%d/%m/%Y, %H:%M:%S (horário de Campo Grande)"
    )
    address = f"{data.street}, {data.number} – {data.district}, CEP {data.postal_code}"
    fields = [
        ("Data/Hora", submitted_at),
        ("Empresa responsável", data.company),
        ("CNPJ", data.company_cnpj or "Não informado"),
        ("Inscrição Municipal", data.municipal_registration),
        ("Inscrição Imobiliária", data.property_registration),
        ("Coordenadas Geográficas", f"{data.latitude:.6f}, {data.longitude:.6f}"),
        ("Endereço do local", address),
        ("Tipo de veículo", MEDIA_LABELS.get(data.media_type, data.media_type)),
    ]
    rows = [[Paragraph(escape(field), label), Paragraph(escape(str(field_value)), value)] for field, field_value in fields]
    table = Table(rows, colWidths=[5 * cm, 12 * cm], hAlign="LEFT")
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LINEBELOW", (0, 0), (-1, -2), 0.3, colors.lightgrey),
    ]))

    story = [
        header, Spacer(1, 0.7 * cm),
        Paragraph(f"Protocolo de Solicitação: {escape(data.process_code)}", title),
        table, Spacer(1, 0.5 * cm),
        Paragraph("Documentos anexados", label), Spacer(1, 0.2 * cm),
    ]
    for attachment in data.attachments:
        category = CATEGORY_LABELS.get(attachment.category, attachment.category)
        story.append(Paragraph(f"• {escape(category)}: {escape(attachment.filename)}", value))
        story.append(Spacer(1, 0.1 * cm))

    story.extend([
        Spacer(1, 0.5 * cm),
        KeepTogether([
            Paragraph(
                "Certifique-se de que seu cadastro no SEI utilize o mesmo e-mail de login informado aqui. "
                "A comunicação sobre o processo será feita pela plataforma SEI como usuário externo.",
                note,
            ),
            Spacer(1, 0.3 * cm),
            Paragraph("Documento oficial de recebimento de dados – SEMADES/SURB/GCP", heading),
        ]),
    ])
    document.build(story)
    return buffer.getvalue()


def send_receipt_email(data: ReceiptData, pdf: bytes, settings: Settings) -> None:
    if not settings.receipt_email_configured:
        raise RuntimeError("Envio de comprovantes por e-mail não configurado.")
    if bool(settings.smtp_username) != bool(settings.smtp_password):
        raise RuntimeError("Credenciais SMTP incompletas.")

    email = EmailMessage()
    email["From"] = f"GCP/SEMADES <{settings.smtp_from_email}>"
    email["To"] = data.requester_email
    email["Subject"] = f"Comprovante de protocolo {data.process_code}"
    email.set_content(
        f"Sua solicitação foi recebida sob o protocolo {data.process_code}.\n\n"
        "O comprovante em PDF está anexado. O acompanhamento do processo é realizado pelo SEI."
    )
    email.add_attachment(pdf, maintype="application", subtype="pdf", filename=f"{data.process_code}.pdf")

    if settings.smtp_use_ssl:
        connection = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=15, context=ssl.create_default_context())
    else:
        connection = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15)
    with connection as smtp:
        if settings.smtp_use_starttls and not settings.smtp_use_ssl:
            smtp.starttls(context=ssl.create_default_context())
        if settings.smtp_username:
            smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(email)
