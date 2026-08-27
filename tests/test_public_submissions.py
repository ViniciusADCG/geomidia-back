import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import HTTPException

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.api.routes.public_submissions import (
    application_form_from_public,
    safe_filename,
    validate_attachment_manifest,
    validate_submission_timing,
)
from app.schemas import PublicAttachmentInput, PublicNewProcessPayload


def valid_public_payload(**overrides):
    data = {
        "tipoProcesso": "PROCESSO_NOVO",
        "email": "requerente@example.com",
        "requerente": {
            "empresa": "Empresa Teste",
            "inscricaoMunicipal": "12345",
        },
        "localInstalacao": {
            "inscricaoImobiliaria": "12345678901",
            "latitude": -20.46,
            "longitude": -54.61,
            "rua": "Avenida Afonso Pena",
            "numero": "1000",
            "bairro": "Centro",
            "cep": "79002-000",
        },
        "veiculoDivulgacao": {
            "tipo": "outdoor",
            "quantidadeFaces": "Duas",
            "areaM2": 12,
            "alturaBordaInferiorM": 4,
        },
        "ciente": True,
        "iniciadoEm": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        "website": "",
    }
    data.update(overrides)
    return PublicNewProcessPayload.model_validate(data)


def attachment(category: str, index: int = 0, *, filename: str = "documento.pdf", content_type: str = "application/pdf"):
    return PublicAttachmentInput.model_validate(
        {
            "idCliente": f"{category}:{index}",
            "categoria": category,
            "nome": filename,
            "tipoConteudo": content_type,
            "tamanhoBytes": 1024,
        }
    )


def valid_manifest():
    return [
        attachment("alvaraLocalizacao"),
        attachment("requerimentoPadrao"),
        attachment("autorizacaoProprietario"),
        attachment("projetoEstrutural"),
        attachment("projetoImplantacao"),
        attachment("artRrt"),
    ]


class PublicSubmissionTests(unittest.TestCase):
    def test_maps_public_payload_to_application_form(self):
        form = application_form_from_public(valid_public_payload())

        self.assertEqual(form.company_responsible, "Empresa Teste")
        self.assertEqual(form.property_registration, "12345678901")
        self.assertEqual(form.media_type.value, "outdoor")
        self.assertEqual(form.number_of_faces, "Duas")

    def test_accepts_required_attachment_manifest(self):
        validate_attachment_manifest(valid_manifest())

    def test_rejects_missing_required_attachment_category(self):
        manifest = [item for item in valid_manifest() if item.category != "artRrt"]

        with self.assertRaises(HTTPException) as context:
            validate_attachment_manifest(manifest)

        self.assertEqual(context.exception.status_code, 422)

    def test_rejects_image_in_pdf_only_category(self):
        manifest = valid_manifest()
        manifest[-1] = attachment("artRrt", filename="foto.png", content_type="image/png")

        with self.assertRaises(HTTPException):
            validate_attachment_manifest(manifest)

    def test_honeypot_is_rejected(self):
        payload = valid_public_payload(website="spam")

        with self.assertRaises(HTTPException) as context:
            validate_submission_timing(payload)

        self.assertEqual(context.exception.status_code, 422)

    def test_filename_is_reduced_to_safe_storage_name(self):
        name = safe_filename("../../Projeto São João (final).PDF")

        self.assertEqual(name, "Projeto-Sao-Joao-final.PDF")
        self.assertNotIn("/", name)


if __name__ == "__main__":
    unittest.main()
