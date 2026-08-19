import sys
import unittest
from pathlib import Path

from pydantic import ValidationError

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.schemas import ApplicationFormCreate, MediaAssetCreate


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

    def test_new_asset_must_start_analysis(self):
        with self.assertRaises(ValidationError):
            MediaAssetCreate.model_validate(valid_asset(status="aprovado"))


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


if __name__ == "__main__":
    unittest.main()
