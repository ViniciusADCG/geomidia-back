import sys
import unittest
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

from pydantic import ValidationError

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.api.routes.media_assets import asset_for_user
from app.db.models import ApplicationForm, MediaAsset, User
from app.schemas import ApplicationFormCreate, ApplicationFormRead, MediaAssetCreate


def valid_asset(**overrides):
    data = {
        "media_type": "outdoor",
        "address": "Av. Afonso Pena, 1000",
        "district": "Santa Fe",
        "latitude": -20.46,
        "longitude": -54.61,
        "area_m2": 27,
        "bottom_height_m": 5,
    }
    data.update(overrides)
    return data


class MediaAssetSchemaTests(unittest.TestCase):
    def test_accepts_modular_electronic_panel(self):
        asset = MediaAssetCreate.model_validate(valid_asset(media_type="painel eletronico modular"))
        self.assertEqual(asset.media_type.value, "painel eletronico modular")

    def test_normalizes_known_district(self):
        asset = MediaAssetCreate.model_validate(valid_asset())
        self.assertEqual(asset.district, "Santa Fé")

    def test_rejects_height_order(self):
        with self.assertRaises(ValidationError):
            MediaAssetCreate.model_validate(valid_asset(top_height_m=3))

    def test_rejects_location_outside_operational_bounds(self):
        with self.assertRaises(ValidationError):
            MediaAssetCreate.model_validate(valid_asset(latitude=-22))

    def test_new_asset_defaults_to_new_processes(self):
        asset = MediaAssetCreate.model_validate(valid_asset())
        self.assertEqual(asset.status.value, "novos processos")

    def test_new_asset_cannot_skip_new_processes(self):
        with self.assertRaises(ValidationError):
            MediaAssetCreate.model_validate(valid_asset(status="análise"))

    def test_accepts_authorization_expiration_date(self):
        asset = MediaAssetCreate.model_validate(valid_asset(expiration_date="2027-05-20"))
        self.assertEqual(asset.expiration_date, date(2027, 5, 20))


class ApplicationFormSchemaTests(unittest.TestCase):
    def valid_form(self, **overrides):
        data = {
            "company_responsible": "Empresa Teste",
            "municipal_registration": "12345",
            "property_registration": "67890",
            "latitude": -20.46,
            "longitude": -54.61,
            "street": "Av. Afonso Pena",
            "number": "1000",
            "district": "Centro",
            "postal_code": "79002-000",
            "media_type": "painel eletronico modular",
            "area_m2": 12,
            "bottom_height_m": 4,
            "requester_email": "requerente@example.com",
            "attachment_links": "https://example.com/documento.pdf",
        }
        data.update(overrides)
        return data

    def test_accepts_complete_application_form(self):
        application_form = ApplicationFormCreate.model_validate(self.valid_form())
        self.assertEqual(application_form.media_type.value, "painel eletronico modular")

    def test_rejects_invalid_postal_code(self):
        with self.assertRaises(ValidationError):
            ApplicationFormCreate.model_validate(self.valid_form(postal_code="7900"))

    def test_rejects_form_outside_operational_bounds(self):
        with self.assertRaises(ValidationError):
            ApplicationFormCreate.model_validate(self.valid_form(latitude=-22))

    def test_read_form_exposes_linked_process_status_and_expiration(self):
        now = datetime.now(UTC)
        asset = MediaAsset(
            id=uuid.uuid4(),
            process_code="PROC-2026-999",
            status="novos processos",
            expiration_date=date(2027, 5, 20),
        )
        application_form = ApplicationForm(
            id=uuid.uuid4(),
            asset_id=asset.id,
            asset=asset,
            created_at=now,
            updated_at=now,
            **self.valid_form(),
        )

        serialized = ApplicationFormRead.model_validate(application_form)

        self.assertEqual(serialized.status.value, "novos processos")
        self.assertEqual(serialized.expiration_date, date(2027, 5, 20))

    def test_media_asset_read_exposes_linked_company_fields(self):
        now = datetime.now(UTC)
        asset = MediaAsset(
            id=uuid.uuid4(),
            process_code="PROC-2026-998",
            media_type="outdoor",
            address="Av. Afonso Pena, 1000",
            district="Centro",
            latitude=-20.46,
            longitude=-54.61,
            area_m2=27,
            bottom_height_m=5,
            radius_meters=80,
            status="novos processos",
            created_at=now,
            updated_at=now,
        )
        asset.application_form = ApplicationForm(
            id=uuid.uuid4(),
            asset_id=asset.id,
            asset=asset,
            created_at=now,
            updated_at=now,
            **self.valid_form(company_responsible="Empresa Filtro", municipal_registration="11222333000144"),
        )
        user = User(id=uuid.uuid4(), username="analista", full_name="Analista", password_hash="unused", role="analyst")

        serialized = asset_for_user(asset, user)

        self.assertEqual(serialized.company_responsible, "Empresa Filtro")
        self.assertEqual(serialized.company_cnpj, "11222333000144")


if __name__ == "__main__":
    unittest.main()
