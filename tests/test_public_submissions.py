import asyncio
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.api.routes.application_forms import asset_data_from_form
from app.api.routes.media_rules import calculate_rule_radius
from app.api.routes.public_submissions import (
    application_form_from_public,
    area_rule_snapshot,
    enforce_public_draft_limit,
    initiate_public_submission,
    matches_file_signature,
    public_vehicle_rules,
    safe_filename,
    validate_attachment_manifest,
    validate_draft_area_rule,
    validate_public_vehicle_rule,
    validate_submission_timing,
    validate_uploaded_attachments,
)
from app.core.config import Settings
from app.db.models import ApplicationForm, MediaAsset, MediaRule
from app.schemas import (
    PUBLIC_ATTACHMENT_MAX_BYTES,
    AreaRuleClassification,
    PublicAttachmentInput,
    PublicNewProcessPayload,
    PublicSubmissionInitiate,
)
from app.services.storage import ObjectMetadata, StorageConfigurationError, SupabaseStorage


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


def attachment(
    category: str, index: int = 0, *, filename: str = "documento.pdf",
    content_type: str = "application/pdf", size_bytes: int = 1024,
):
    return PublicAttachmentInput.model_validate(
        {
            "idCliente": f"{category}:{index}",
            "categoria": category,
            "nome": filename,
            "tipoConteudo": content_type,
            "tamanhoBytes": size_bytes,
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
    def test_public_attachment_schema_accepts_25_mib_and_rejects_larger_file(self):
        self.assertEqual(PUBLIC_ATTACHMENT_MAX_BYTES, 26_214_400)
        self.assertEqual(attachment("artRrt", size_bytes=PUBLIC_ATTACHMENT_MAX_BYTES).size_bytes, PUBLIC_ATTACHMENT_MAX_BYTES)
        with self.assertRaises(ValidationError):
            attachment("artRrt", size_bytes=PUBLIC_ATTACHMENT_MAX_BYTES + 1)

    def test_uploaded_content_must_match_file_type(self):
        valid = {
            "a.pdf": b"%PDF-1.7\n",
            "a.jpg": b"\xff\xd8\xff\xe0",
            "a.png": b"\x89PNG\r\n\x1a\n",
            "a.gif": b"GIF89a",
            "a.webp": b"RIFF\x00\x00\x00\x00WEBP",
            "a.bmp": b"BM\x00\x00",
            "a.heic": b"\x00\x00\x00\x18ftypheic",
            "a.heif": b"\x00\x00\x00\x18ftypmif1",
        }
        for filename, signature in valid.items():
            with self.subTest(filename=filename):
                self.assertTrue(matches_file_signature(filename, signature))
                self.assertFalse(matches_file_signature(filename, b"<html>not an upload</html>"))
        self.assertFalse(matches_file_signature("a.pdf", valid["a.png"]))
        self.assertFalse(matches_file_signature("a.png", valid["a.pdf"]))

    def test_rejects_mismatched_declared_image_type(self):
        manifest = valid_manifest()
        manifest[0] = attachment("alvaraLocalizacao", filename="foto.jpg", content_type="image/png")
        with self.assertRaises(HTTPException):
            validate_attachment_manifest(manifest)

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
                    "areaRuleThresholdM2": 5,
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

    def test_changed_rule_during_draft_requires_new_area_answer(self):
        original = MediaRule(base_radius_meters=250, area_threshold_m2=5, radius_above_threshold_meters=1000)
        current = MediaRule(base_radius_meters=250, area_threshold_m2=8, radius_above_threshold_meters=1000)
        payload = valid_public_payload(veiculoDivulgacao={
            "tipo": "painel de led", "quantidadeFaces": "Duas", "areaRuleClassification": "above_limit",
            "areaRuleThresholdM2": 5,
        })
        draft = SimpleNamespace(payload={"_area_rule_snapshot": area_rule_snapshot(original)})
        with self.assertRaises(HTTPException) as caught:
            validate_draft_area_rule(draft, current, payload)
        self.assertEqual(caught.exception.status_code, 409)
        with self.assertRaises(HTTPException):
            validate_public_vehicle_rule(current, payload)
        validate_draft_area_rule(draft, original, payload)

    def test_legacy_area_classification_uses_current_rule_without_inventing_area(self):
        payload = valid_public_payload(veiculoDivulgacao={
            "tipo": "painel de led", "quantidadeFaces": "Duas", "areaRuleClassification": "above_limit",
        })
        rule = MediaRule(base_radius_meters=250, area_threshold_m2=5, radius_above_threshold_meters=1000)
        validate_public_vehicle_rule(rule, payload)
        self.assertEqual(calculate_rule_radius(rule, payload.vehicle.area_m2,
                                               payload.vehicle.area_rule_classification), 1000)
        self.assertIsNone(payload.vehicle.area_m2)
        self.assertEqual(payload.vehicle.area_rule_classification, AreaRuleClassification.above_limit)

    def test_legacy_draft_without_snapshot_needs_unchanged_rule(self):
        created_at = datetime.now(UTC)
        rule = MediaRule(
            base_radius_meters=250, area_threshold_m2=5, radius_above_threshold_meters=1000,
            created_at=created_at - timedelta(days=1), updated_at=created_at - timedelta(minutes=1),
        )
        payload = valid_public_payload(veiculoDivulgacao={
            "tipo": "painel de led", "quantidadeFaces": "Duas", "areaRuleClassification": "above_limit",
        })
        draft = SimpleNamespace(payload={}, created_at=created_at)
        validate_draft_area_rule(draft, rule, payload)
        rule.updated_at = created_at + timedelta(seconds=1)
        with self.assertRaises(HTTPException) as changed:
            validate_draft_area_rule(draft, rule, payload)
        self.assertEqual(changed.exception.status_code, 409)
        self.assertIn("regra de area mudou", changed.exception.detail)

        rule.updated_at = None
        with self.assertRaises(HTTPException) as unknown:
            validate_draft_area_rule(draft, rule, payload)
        self.assertEqual(unknown.exception.status_code, 409)
        rule.updated_at = created_at
        with self.assertRaises(HTTPException):
            validate_draft_area_rule(draft, rule, payload)
        rule.updated_at = created_at - timedelta(minutes=1)
        rule.created_at = created_at + timedelta(seconds=1)
        with self.assertRaises(HTTPException):
            validate_draft_area_rule(draft, rule, payload)

    def test_fixed_radius_legacy_draft_needs_no_rule_snapshot(self):
        payload = valid_public_payload(veiculoDivulgacao={"tipo": "outdoor", "quantidadeFaces": "Duas"})
        draft = SimpleNamespace(payload={}, created_at=datetime.now(UTC))
        rule = MediaRule(base_radius_meters=80, area_threshold_m2=None, radius_above_threshold_meters=None)
        validate_draft_area_rule(draft, rule, payload)
        validate_public_vehicle_rule(rule, payload)

    def test_public_new_asset_keeps_coordinates_and_uses_current_radius(self):
        payload = valid_public_payload(veiculoDivulgacao={
            "tipo": "painel de led", "quantidadeFaces": "Duas", "areaRuleClassification": "above_limit",
            "areaRuleThresholdM2": 5,
        })
        form = application_form_from_public(payload)
        rule = MediaRule(base_radius_meters=250, area_threshold_m2=5, radius_above_threshold_meters=1000)
        validate_public_vehicle_rule(rule, payload)
        self.assertEqual((form.latitude, form.longitude), (-20.46, -54.61))
        self.assertEqual(calculate_rule_radius(rule, form.area_m2, form.area_rule_classification), 1000)

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


class PublicRolloutCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_initiation_persists_current_rule_snapshot(self):
        payload = valid_public_payload(veiculoDivulgacao={
            "tipo": "painel de led", "quantidadeFaces": "Duas", "areaRuleClassification": "within_limit",
        })
        body = PublicSubmissionInitiate(payload=payload, attachments=valid_manifest())
        rule = MediaRule(base_radius_meters=250, area_threshold_m2=5, radius_above_threshold_meters=1000)
        storage = SimpleNamespace(create_signed_upload=AsyncMock(
            return_value=SimpleNamespace(signed_url="https://upload.example.test/file"),
        ))
        session = SimpleNamespace(add=MagicMock(), commit=AsyncMock())
        request = Request({
            "type": "http", "headers": [(b"origin", b"http://localhost:5173")],
            "client": ("127.0.0.1", 1234),
        })
        with patch("app.api.routes.public_submissions.active_rule_for_type", new_callable=AsyncMock,
                   return_value=rule), patch(
            "app.api.routes.public_submissions.enforce_public_draft_limit", new_callable=AsyncMock,
        ), patch("app.api.routes.public_submissions.storage_or_503", return_value=storage):
            result = await initiate_public_submission(body, request, session)
        draft = session.add.call_args.args[0]
        self.assertEqual(result.draft_id, draft.id)
        self.assertEqual(draft.payload["_area_rule_snapshot"], area_rule_snapshot(rule))
        self.assertIsNone(draft.payload["vehicle"]["area_m2"])
        self.assertEqual(draft.payload["vehicle"]["area_rule_classification"], "within_limit")
        self.assertIsNone(draft.payload["vehicle"]["area_rule_threshold_m2"])
        session.commit.assert_awaited_once()


class PublicUploadSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def test_storage_accepts_signed_pdf_above_old_limit_through_25_mib(self):
        for size in (11 * 1024 * 1024, PUBLIC_ATTACHMENT_MAX_BYTES):
            with self.subTest(size=size):
                records = [{"object_path": "draft/file.pdf", "filename": "file.pdf", "size_bytes": size}]
                storage = SimpleNamespace(
                    get_object_metadata=AsyncMock(return_value=ObjectMetadata(size, "application/pdf")),
                    get_object_signature=AsyncMock(return_value=b"%PDF-1.7"),
                )
                await validate_uploaded_attachments(storage, records)
                storage.get_object_signature.assert_awaited_once()

    async def test_storage_rejects_file_above_25_mib(self):
        size = PUBLIC_ATTACHMENT_MAX_BYTES + 1
        records = [{"object_path": "draft/file.pdf", "filename": "file.pdf", "size_bytes": PUBLIC_ATTACHMENT_MAX_BYTES}]
        storage = SimpleNamespace(
            get_object_metadata=AsyncMock(return_value=ObjectMetadata(size, "application/pdf")),
            get_object_signature=AsyncMock(),
        )
        with self.assertRaises(HTTPException) as context:
            await validate_uploaded_attachments(storage, records)
        self.assertEqual(context.exception.status_code, 409)
        self.assertIn("25 MiB", context.exception.detail)
        storage.get_object_signature.assert_not_awaited()

    async def test_storage_reads_only_signature_from_authenticated_object(self):
        seen = []

        def respond(request):
            seen.append(request)
            return httpx.Response(200, content=b"%PDF-" + b"x" * 1000)

        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        storage = SupabaseStorage(Settings(supabase_url="https://example.supabase.co", supabase_service_role_key="sb_secret_example"))
        with patch("app.services.storage.httpx.AsyncClient", return_value=client):
            signature = await storage.get_object_signature("draft/file.pdf")
        self.assertEqual(len(signature), 32)
        self.assertEqual(seen[0].headers["range"], "bytes=0-31")
        self.assertEqual(seen[0].url.path, "/storage/v1/object/authenticated/application-form-attachments/draft/file.pdf")

    async def test_rejects_changed_content_even_when_size_matches(self):
        records = [{"object_path": "draft/file.pdf", "filename": "file.pdf", "size_bytes": 1024}]
        storage = SimpleNamespace(
            get_object_metadata=AsyncMock(return_value=ObjectMetadata(1024, "application/pdf")),
            get_object_signature=AsyncMock(return_value=b"<html>"),
        )
        with self.assertRaises(HTTPException) as context:
            await validate_uploaded_attachments(storage, records)
        self.assertEqual(context.exception.status_code, 409)

    async def test_rejects_unverifiable_size_before_reading_content(self):
        records = [{"object_path": "draft/file.pdf", "filename": "file.pdf", "size_bytes": 1024}]
        storage = SimpleNamespace(
            get_object_metadata=AsyncMock(return_value=ObjectMetadata(None, "application/pdf")),
            get_object_signature=AsyncMock(),
        )
        with self.assertRaises(HTTPException) as context:
            await validate_uploaded_attachments(storage, records)
        self.assertEqual(context.exception.status_code, 409)
        storage.get_object_signature.assert_not_awaited()

    async def test_draft_limit_takes_lock_before_count(self):
        calls = []

        async def execute(_statement):
            calls.append("lock")

        async def scalar(_statement):
            calls.append("count")
            return 5

        session = SimpleNamespace(execute=execute, scalar=scalar)
        with self.assertRaises(HTTPException) as context:
            await enforce_public_draft_limit(session, "a" * 64, Settings(public_submission_rate_limit=5))
        self.assertEqual(context.exception.status_code, 429)
        self.assertEqual(calls, ["lock", "count"])


if __name__ == "__main__":
    unittest.main()
