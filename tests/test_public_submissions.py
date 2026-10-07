import asyncio
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import HTTPException
from pydantic import ValidationError

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.api.routes.application_forms import asset_data_from_form
from app.api.routes.public_submissions import (
    application_form_from_public,
    public_vehicle_rules,
    safe_filename,
    validate_attachment_manifest,
    validate_public_vehicle_rule,
    validate_submission_timing,
)
from app.core.config import Settings
from app.db.models import ApplicationForm, MediaAsset, MediaRule
from app.schemas import AreaRuleClassification, PublicAttachmentInput, PublicNewProcessPayload
from app.services.storage import StorageConfigurationError, SupabaseStorage


def valid_public_payload(**overrides):
    data = {
        "tipoProcesso": "PROCESSO_NOVO",
        "email": "requerente@example.com",
        "requerente": {
            "empresa": "Empresa Teste",
            "cnpj": "11222333000144",
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
    def test_public_rules_expose_active_area_threshold(self):
        rule = MediaRule(media_type="painel de led", area_threshold_m2=7, is_active=True)
        session = SimpleNamespace(scalars=AsyncMock(return_value=[rule]))
        self.assertEqual(
            asyncio.run(public_vehicle_rules(session)),
            [{"tipo": "painel de led", "limiteAreaM2": 7}],
        )

    def test_fixed_radius_vehicle_needs_no_measurements_or_classification(self):
        payload = valid_public_payload(veiculoDivulgacao={"tipo": "outdoor", "quantidadeFaces": "Duas"})
        rule = MediaRule(base_radius_meters=80, area_threshold_m2=None, radius_above_threshold_meters=None)
        validate_public_vehicle_rule(rule, payload)
        form = application_form_from_public(payload)
        self.assertIsNone(form.area_m2)
        self.assertIsNone(form.bottom_height_m)
        self.assertIsNone(form.area_rule_classification)

    def test_area_classification_reaches_both_persisted_models(self):
        rule = MediaRule(base_radius_meters=250, area_threshold_m2=5, radius_above_threshold_meters=1000)
        for value in ("within_limit", "above_limit"):
            with self.subTest(value=value):
                payload = valid_public_payload(veiculoDivulgacao={
                    "tipo": "painel de led", "quantidadeFaces": "Duas", "areaRuleClassification": value,
                })
                payload = PublicNewProcessPayload.model_validate(payload.model_dump(mode="json"))
                validate_public_vehicle_rule(rule, payload)
                form = application_form_from_public(payload)
                form_row = ApplicationForm(**form.model_dump(mode="json", exclude={"expiration_date"}))
                asset_row = MediaAsset(**asset_data_from_form(form))
                self.assertIsNone(form_row.area_m2)
                self.assertIsNone(asset_row.bottom_height_m)
                self.assertEqual(form.area_rule_classification, AreaRuleClassification(value))
                self.assertEqual(form_row.area_rule_classification, value)
                self.assertEqual(asset_row.area_rule_classification, value)

    def test_area_rule_rejects_missing_classification(self):
        rule = MediaRule(base_radius_meters=250, area_threshold_m2=5, radius_above_threshold_meters=1000)
        payload = valid_public_payload(veiculoDivulgacao={"tipo": "painel de led", "quantidadeFaces": "Duas"})
        with self.assertRaises(HTTPException):
            validate_public_vehicle_rule(rule, payload)

    def test_fixed_rule_rejects_unneeded_classification(self):
        rule = MediaRule(base_radius_meters=80, area_threshold_m2=None, radius_above_threshold_meters=None)
        payload = valid_public_payload(veiculoDivulgacao={
            "tipo": "outdoor", "quantidadeFaces": "Duas", "areaRuleClassification": "within_limit",
        })
        with self.assertRaises(HTTPException):
            validate_public_vehicle_rule(rule, payload)

    def test_maps_public_payload_to_application_form(self):
        form = application_form_from_public(valid_public_payload())

        self.assertEqual(form.company_responsible, "Empresa Teste")
        self.assertEqual(form.company_cnpj, "11222333000144")
        self.assertEqual(form.property_registration, "12345678901")
        self.assertEqual(form.media_type.value, "outdoor")
        self.assertEqual(form.number_of_faces, "Duas")

    def test_rejects_invalid_company_cnpj(self):
        with self.assertRaises(ValidationError):
            valid_public_payload(
                requerente={
                    "empresa": "Empresa Teste",
                    "cnpj": "123",
                    "inscricaoMunicipal": "12345",
                }
            )

    def test_rejects_public_location_outside_municipality(self):
        with self.assertRaises(ValidationError):
            valid_public_payload(
                localInstalacao={
                    "inscricaoImobiliaria": "12345678901",
                    "latitude": -20.40,
                    "longitude": -54.79,
                    "rua": "Avenida Afonso Pena",
                    "numero": "1000",
                    "bairro": "Centro",
                    "cep": "79002-000",
                }
            )

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

    def test_current_supabase_secret_uses_only_apikey_header(self):
        storage = SupabaseStorage(
            Settings(supabase_url="https://example.supabase.co", supabase_service_role_key="sb_secret_example")
        )

        self.assertEqual(storage.headers, {"apikey": "sb_secret_example"})

    def test_legacy_service_role_key_is_also_sent_as_bearer(self):
        storage = SupabaseStorage(
            Settings(supabase_url="https://example.supabase.co", supabase_service_role_key="legacy-jwt")
        )

        self.assertEqual(
            storage.headers,
            {"apikey": "legacy-jwt", "Authorization": "Bearer legacy-jwt"},
        )

    def test_supabase_url_requires_protocol(self):
        with self.assertRaises(StorageConfigurationError):
            SupabaseStorage(Settings(supabase_url="example.supabase.co", supabase_service_role_key="sb_secret_example"))


if __name__ == "__main__":
    unittest.main()
