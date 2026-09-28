import unittest
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from app.api.routes.requirement_responses import (
    download_requirement_receipt,
    finalize_requirement_response,
    next_requirement_response_code,
    validate_requirement_submission,
)
from app.core.config import Settings
from app.schemas import PublicSubmissionFinalize, RequirementResponseInitiate
from app.services.protocols import protocol_year
from app.services.requirement_receipt import (
    RequirementReceiptData,
    generate_requirement_receipt_pdf,
    send_requirement_receipt_email,
)


def sample_data() -> RequirementReceiptData:
    return RequirementReceiptData(
        protocol="HESP-0272-2026",
        finalized_at=datetime(2026, 9, 25, 16, 44, 11, tzinfo=UTC),
        process_number="141115/2026-09",
        notice_number="2143391",
        requester_email="requerente@example.com",
        filenames=("resposta.pdf",),
    )


def sample_request(**overrides) -> RequirementResponseInitiate:
    payload = {
        "tipoProcesso": "RESPOSTA_COMUNICADO_EXIGENCIA",
        "email": "requerente@example.com",
        "numeroProcesso": "141115/2026-09",
        "numeroComunicado": "2143391",
        "ciente": True,
        "iniciadoEm": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        "website": "",
    }
    payload.update(overrides)
    return RequirementResponseInitiate.model_validate({
        "payload": payload,
        "arquivos": [{
            "idCliente": "respostaExigencia:0", "categoria": "respostaExigencia",
            "nome": "resposta.pdf", "tipoConteudo": "application/pdf", "tamanhoBytes": 1024,
        }],
    })


class RequirementReceiptTests(unittest.TestCase):
    def test_pdf_is_generated_with_hesp_protocol(self):
        pdf = generate_requirement_receipt_pdf(sample_data())
        self.assertTrue(pdf.startswith(b"%PDF-"))
        self.assertGreater(len(pdf), 3000)

    def test_email_attaches_the_hesp_pdf(self):
        settings = Settings(smtp_host="smtp.example.com", smtp_from_email="protocolos@example.com")
        smtp = MagicMock()
        smtp.__enter__.return_value = smtp
        with patch("app.services.requirement_receipt.smtplib.SMTP", return_value=smtp):
            send_requirement_receipt_email(sample_data(), b"%PDF-example", settings)
        message = smtp.send_message.call_args.args[0]
        self.assertEqual(message["To"], "requerente@example.com")
        self.assertEqual(list(message.iter_attachments())[0].get_filename(), "HESP-0272-2026.pdf")

    def test_validates_response_and_rejects_wrong_attachment_category(self):
        valid = sample_request()
        validate_requirement_submission(valid)
        valid.attachments[0].category = "artRrt"
        with self.assertRaises(HTTPException):
            validate_requirement_submission(valid)

    def test_rejects_blank_notice_number(self):
        with self.assertRaises(ValidationError):
            sample_request(numeroComunicado="   ")

    def test_rejects_response_without_sei_acknowledgement(self):
        with self.assertRaises(HTTPException):
            validate_requirement_submission(sample_request(ciente=False))


class RequirementFinalizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_response_of_a_new_year_uses_hesp_01(self):
        session = SimpleNamespace(scalars=AsyncMock(return_value=[]), scalar=AsyncMock(return_value=1))
        code = await next_requirement_response_code(session, 2027)
        self.assertEqual(code, "HESP-01-2027")

    async def test_finalizing_assigns_hesp_protocol_only_after_uploaded_file_is_verified(self):
        token = "x" * 32
        draft = SimpleNamespace(
            payload=sample_request().payload.model_dump(mode="json"),
            token_hash=sha256(token.encode()).hexdigest(),
            attachments=[{"object_path": "public-submissions/example/resposta.pdf", "filename": "resposta.pdf", "size_bytes": 1024}],
            created_at=datetime.now(UTC) - timedelta(minutes=1),
            finalized_at=None, process_code=None, receipt_sent_at=None,
        )
        session = SimpleNamespace(
            scalar=AsyncMock(side_effect=[draft, 1]),
            scalars=AsyncMock(return_value=[]),
            commit=AsyncMock(),
        )
        storage = SimpleNamespace(get_object_metadata=AsyncMock(return_value=SimpleNamespace(size_bytes=1024)))
        request = Request({
            "type": "http", "headers": [(b"origin", b"http://localhost:5173")],
            "client": ("127.0.0.1", 1234),
        })
        with (
            patch("app.api.routes.requirement_responses.storage_or_503", return_value=storage),
            patch("app.api.routes.requirement_responses.send_finalized_requirement_receipt", new_callable=AsyncMock) as send,
        ):
            result = await finalize_requirement_response(uuid4(), PublicSubmissionFinalize(token=token), request, session)
        storage.get_object_metadata.assert_awaited_once()
        session.commit.assert_awaited_once()
        self.assertEqual(result.protocolo, f"HESP-01-{protocol_year()}")
        self.assertFalse(result.receipt_sent)
        send.assert_not_awaited()

    async def test_finalized_response_pdf_requires_the_draft_token(self):
        token = "x" * 32
        draft = SimpleNamespace(
            token_hash=sha256(token.encode()).hexdigest(),
            payload=sample_request().payload.model_dump(mode="json"),
            attachments=[{"filename": "resposta.pdf"}],
            finalized_at=datetime.now(UTC), process_code="HESP-01-2026",
        )
        session = SimpleNamespace(scalar=AsyncMock(return_value=draft))
        request = Request({
            "type": "http", "headers": [(b"origin", b"http://localhost:5173")],
            "client": ("127.0.0.1", 1234),
        })
        response = await download_requirement_receipt(uuid4(), PublicSubmissionFinalize(token=token), request, session)
        self.assertTrue(response.body.startswith(b"%PDF-"))
        self.assertIn("HESP-01-2026.pdf", response.headers["content-disposition"])
        with self.assertRaises(HTTPException) as denied:
            await download_requirement_receipt(uuid4(), PublicSubmissionFinalize(token="z" * 32), request, session)
        self.assertEqual(denied.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
