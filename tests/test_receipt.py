import unittest
from datetime import UTC, datetime
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from fastapi import HTTPException
from starlette.requests import Request

from app.api.routes.public_submissions import (
    download_public_receipt,
    finalize_public_submission,
    next_public_process_code,
    result_for_receipt,
    send_finalized_receipt,
)
from app.core.config import Settings
from app.schemas import PublicSubmissionFinalize
from app.services.receipt import ReceiptAttachment, ReceiptData, generate_receipt_pdf, send_receipt_email


def sample_receipt() -> ReceiptData:
    return ReceiptData(
        process_code="VEI-0044-2026",
        finalized_at=datetime(2026, 5, 4, 14, 12, 13, tzinfo=UTC),
        requester_email="requerente@example.com",
        company="Empresa Exemplo",
        company_cnpj="11222333000181",
        municipal_registration="12345",
        property_registration="12345678901",
        latitude=-20.46,
        longitude=-54.61,
        street="Avenida Afonso Pena",
        number="1000",
        district="Centro",
        postal_code="79002-000",
        media_type="front light",
        attachments=(ReceiptAttachment("requerimentoPadrao", "Requerimento.pdf"),),
    )


class ReceiptTests(unittest.TestCase):
    def test_generates_nonempty_pdf_from_process_data(self):
        pdf = generate_receipt_pdf(sample_receipt())

        self.assertTrue(pdf.startswith(b"%PDF-"))
        self.assertGreater(len(pdf), 3000)

    def test_email_uses_requester_as_recipient_and_attaches_pdf(self):
        settings = Settings(
            smtp_host="smtp.example.com",
            smtp_from_email="protocolos@example.com",
            smtp_username="protocolos@example.com",
            smtp_password="test-password",
        )
        smtp = MagicMock()
        smtp.__enter__.return_value = smtp
        with patch("app.services.receipt.smtplib.SMTP", return_value=smtp):
            send_receipt_email(sample_receipt(), b"%PDF-example", settings)

        message = smtp.send_message.call_args.args[0]
        self.assertEqual(message["To"], "requerente@example.com")
        self.assertEqual(message["Subject"], "Comprovante de protocolo VEI-0044-2026")
        attachment = list(message.iter_attachments())[0]
        self.assertEqual(attachment.get_filename(), "VEI-0044-2026.pdf")
        self.assertEqual(attachment.get_payload(decode=True), b"%PDF-example")
        smtp.starttls.assert_called_once()
        smtp.login.assert_called_once_with("protocolos@example.com", "test-password")

    def test_api_response_reports_actual_delivery_status(self):
        result = result_for_receipt("VEI-01-2026")
        self.assertFalse(result.model_dump(by_alias=True)["comprovanteEnviado"])
        self.assertIn("Baixe o comprovante PDF", result.message)


class ReceiptDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeating_a_finalized_submission_does_not_send_email(self):
        token = "x" * 32
        draft = SimpleNamespace(
            token_hash=sha256(token.encode()).hexdigest(),
            finalized_at=datetime.now(UTC),
            process_code="VEI-0044-2026",
        )
        session = SimpleNamespace(scalar=AsyncMock(return_value=draft), commit=AsyncMock())
        request = Request({
            "type": "http",
            "headers": [(b"origin", b"http://localhost:5173")],
            "client": ("127.0.0.1", 1234),
        })
        with patch("app.api.routes.public_submissions.send_finalized_receipt", new_callable=AsyncMock) as send:
            result = await finalize_public_submission(
                uuid4(), PublicSubmissionFinalize(token=token), request, session
            )

        self.assertFalse(result.receipt_sent)
        self.assertEqual(result.protocolo, "VEI-0044-2026")
        send.assert_not_awaited()
        session.commit.assert_not_awaited()

    async def test_first_public_process_of_the_year_uses_vei_01(self):
        session = SimpleNamespace(scalars=AsyncMock(return_value=[]), scalar=AsyncMock(return_value=1))
        code = await next_public_process_code(session, 2027)
        self.assertEqual(code, "VEI-01-2027")

    async def test_finalized_process_receipt_is_available_with_its_token(self):
        token = "x" * 32
        draft = SimpleNamespace(
            token_hash=sha256(token.encode()).hexdigest(),
            finalized_at=datetime.now(UTC), process_code="VEI-01-2026",
        )
        form = SimpleNamespace(
            requester_email="requerente@example.com", company_responsible="Empresa Exemplo",
            company_cnpj="11222333000181", municipal_registration="12345", property_registration="12345678901",
            latitude=-20.46, longitude=-54.61, street="Avenida Afonso Pena", number="1000",
            district="Centro", postal_code="79002-000", media_type="outdoor", attachments=[],
        )
        session = SimpleNamespace(scalar=AsyncMock(side_effect=[draft, form]))
        request = Request({
            "type": "http", "headers": [(b"origin", b"http://localhost:5173")],
            "client": ("127.0.0.1", 1234),
        })
        response = await download_public_receipt(uuid4(), PublicSubmissionFinalize(token=token), request, session)
        self.assertTrue(response.body.startswith(b"%PDF-"))
        self.assertIn("VEI-01-2026.pdf", response.headers["content-disposition"])

        denied_session = SimpleNamespace(scalar=AsyncMock(return_value=draft))
        with self.assertRaises(HTTPException) as denied:
            await download_public_receipt(
                uuid4(), PublicSubmissionFinalize(token="z" * 32), request, denied_session
            )
        self.assertEqual(denied.exception.status_code, 404)

    async def test_already_sent_receipt_is_not_sent_twice(self):
        draft = SimpleNamespace(receipt_sent_at=datetime.now(UTC))
        session = SimpleNamespace(scalar=AsyncMock(), commit=AsyncMock())

        sent = await send_finalized_receipt(draft, session, Settings())

        self.assertTrue(sent)
        session.scalar.assert_not_awaited()
        session.commit.assert_not_awaited()

    async def test_missing_smtp_keeps_process_without_claiming_email_was_sent(self):
        draft = SimpleNamespace(receipt_sent_at=None)
        session = SimpleNamespace(scalar=AsyncMock(), commit=AsyncMock())

        sent = await send_finalized_receipt(draft, session, Settings())

        self.assertFalse(sent)
        session.scalar.assert_not_awaited()
        session.commit.assert_not_awaited()

    async def test_success_marks_receipt_sent(self):
        draft = SimpleNamespace(process_code="VEI-0044-2026", finalized_at=datetime.now(UTC), receipt_sent_at=None)
        form = SimpleNamespace(
            requester_email="requerente@example.com",
            company_responsible="Empresa Exemplo",
            company_cnpj="11222333000181",
            municipal_registration="12345",
            property_registration="12345678901",
            latitude=-20.46,
            longitude=-54.61,
            street="Avenida Afonso Pena",
            number="1000",
            district="Centro",
            postal_code="79002-000",
            media_type="front light",
            attachments=[SimpleNamespace(category="requerimentoPadrao", original_filename="Requerimento.pdf")],
        )
        session = SimpleNamespace(scalar=AsyncMock(return_value=form), commit=AsyncMock())
        settings = Settings(smtp_host="smtp.example.com", smtp_from_email="protocolos@example.com")
        with patch("app.api.routes.public_submissions.generate_receipt_pdf", return_value=b"%PDF-example"), patch(
            "app.api.routes.public_submissions.send_receipt_email"
        ) as send:
            sent = await send_finalized_receipt(draft, session, settings)

        self.assertTrue(sent)
        self.assertIsNotNone(draft.receipt_sent_at)
        self.assertEqual(send.call_args.args[0].requester_email, "requerente@example.com")
        session.commit.assert_awaited_once()

    async def test_failed_delivery_can_be_retried(self):
        draft = SimpleNamespace(process_code="VEI-0044-2026", finalized_at=datetime.now(UTC), receipt_sent_at=None)
        form = SimpleNamespace(**{
            "requester_email": "requerente@example.com", "company_responsible": "Empresa Exemplo",
            "company_cnpj": None, "municipal_registration": "12345", "property_registration": "12345678901",
            "latitude": -20.46, "longitude": -54.61, "street": "Rua A", "number": "1",
            "district": "Centro", "postal_code": "79002-000", "media_type": "outdoor", "attachments": [],
        })
        session = SimpleNamespace(scalar=AsyncMock(return_value=form), commit=AsyncMock())
        settings = Settings(smtp_host="smtp.example.com", smtp_from_email="protocolos@example.com")
        with patch("app.api.routes.public_submissions.generate_receipt_pdf", return_value=b"%PDF-example"), patch(
            "app.api.routes.public_submissions.send_receipt_email", side_effect=OSError("SMTP unavailable")
        ):
            sent = await send_finalized_receipt(draft, session, settings)

        self.assertFalse(sent)
        self.assertIsNone(draft.receipt_sent_at)
        session.commit.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
