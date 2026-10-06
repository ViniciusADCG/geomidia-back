"""HESP receipt for a response to a notice of requirements."""

import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from html import escape
from io import BytesIO
from zoneinfo import ZoneInfo

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import HRFlowable, Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.core.config import Settings
from app.services.receipt import ASSET_PATH


@dataclass(frozen=True)
class RequirementReceiptData:
    protocol: str
    finalized_at: datetime
    process_number: str
    notice_number: str
    requester_email: str
    filenames: tuple[str, ...]


def generate_requirement_receipt_pdf(data: RequirementReceiptData) -> bytes:
    """Render the HESP receipt using finalized, server-side data."""
    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer, pagesize=(21 * cm, 29.7 * cm), leftMargin=2 * cm,
        rightMargin=2 * cm, topMargin=1.4 * cm, bottomMargin=1.5 * cm,
        title=f"Resposta de Comunicado de Exigência: {data.protocol}",
        author="SEMADES/SURB/GCP",
    )
    styles = getSampleStyleSheet()
    heading = ParagraphStyle("HespHeading", parent=styles["Normal"], fontName="Helvetica-Bold",
                             fontSize=8, leading=11, alignment=TA_CENTER)
    title = ParagraphStyle("HespTitle", parent=styles["Heading1"], fontName="Helvetica-Bold",
                           fontSize=14, leading=18, alignment=TA_CENTER)
    section = ParagraphStyle("HespSection", parent=styles["Normal"], fontName="Helvetica-Bold",
                             fontSize=11, leading=15)
    label = ParagraphStyle("HespLabel", parent=styles["Normal"], fontName="Helvetica-Bold",
                           fontSize=9, leading=13)
    body = ParagraphStyle("HespBody", parent=styles["Normal"], fontSize=9, leading=13,
                          wordWrap="CJK")
    center = ParagraphStyle("HespCenter", parent=body, alignment=TA_CENTER)

    header = Table([[
        Image(str(ASSET_PATH), width=1.6 * cm, height=1.6 * cm),
        Paragraph(
            "PREFEITURA MUNICIPAL DE CAMPO GRANDE<br/>ESTADO DE MATO GROSSO DO SUL<br/>"
            "SECRETARIA MUNICIPAL DE MEIO AMBIENTE, GESTÃO URBANA E DESENVOLVIMENTO "
            "ECONÔMICO, TURÍSTICO E SUSTENTÁVEL – SEMADES<br/>"
            "SUPERINTENDÊNCIA DE URBANISMO – SURB<br/>"
            "GERÊNCIA DE CONTROLE DE POSTURAS – GCP", heading,
        ),
    ]], colWidths=[2 * cm, 15 * cm])
    header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    finalized = data.finalized_at.astimezone(ZoneInfo("America/Campo_Grande")).strftime("%d/%m/%Y, %H:%M:%S")
    rows = [
        ("Número do processo:", data.process_number),
        ("Número do Comunicado de Exigência:", data.notice_number),
        ("E-mail do requerente:", data.requester_email),
    ]
    details = Table([
        [Paragraph(escape(key), label), Paragraph(escape(value), body)] for key, value in rows
    ], colWidths=[6.3 * cm, 10.7 * cm], hAlign="LEFT")
    details.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    story = [
        header, Spacer(1, 0.35 * cm), HRFlowable(width="100%", thickness=0.5, color=colors.grey),
        Spacer(1, 0.65 * cm),
        Paragraph(f"Resposta de Comunicado de Exigência: {escape(data.protocol)}", title),
        Spacer(1, 0.15 * cm), Paragraph(f"Data/Hora: {finalized}", center),
        Spacer(1, 0.75 * cm), Paragraph("Resposta de Comunicado de Exigência", section),
        Spacer(1, 0.3 * cm), details, Spacer(1, 0.45 * cm),
        Paragraph("Documentos anexados:", label), Spacer(1, 0.15 * cm),
    ]
    for filename in data.filenames:
        story.extend((Paragraph(f"• {escape(filename)}", body), Spacer(1, 0.12 * cm)))
    story.extend([
        Spacer(1, 0.7 * cm),
        Paragraph("Certifique-se de que seu cadastro no SEI utilize o mesmo e-mail de login informado aqui.", body),
        Spacer(1, 0.15 * cm),
        Paragraph("Estou ciente que a comunicação será feita exclusivamente pela plataforma SEI como usuário externo.", body),
        Spacer(1, 0.45 * cm),
        Paragraph("Documento oficial de recebimento de dados - SEMADES/SURB/GCP", center),
    ])
    document.build(story)
    return buffer.getvalue()


def send_requirement_receipt_email(data: RequirementReceiptData, pdf: bytes, settings: Settings) -> None:
    if not settings.receipt_email_configured:
        raise RuntimeError("Envio de comprovantes por e-mail não configurado.")
    if bool(settings.smtp_username) != bool(settings.smtp_password):
        raise RuntimeError("Credenciais SMTP incompletas.")
    message = EmailMessage()
    message["From"] = f"GCP/SEMADES <{settings.smtp_from_email}>"
    message["To"] = data.requester_email
    message["Subject"] = f"Comprovante de resposta {data.protocol}"
    message.set_content(
        f"Sua resposta ao comunicado de exigência foi recebida sob o protocolo {data.protocol}.\n\n"
        "O comprovante em PDF está anexado. A comunicação do processo é feita pela plataforma SEI."
    )
    message.add_attachment(pdf, maintype="application", subtype="pdf", filename=f"{data.protocol}.pdf")
    if settings.smtp_use_ssl:
        connection = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=15, context=ssl.create_default_context())
    else:
        connection = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15)
    with connection as smtp:
        if settings.smtp_use_starttls and not settings.smtp_use_ssl:
            smtp.starttls(context=ssl.create_default_context())
        if settings.smtp_username:
            smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(message)
