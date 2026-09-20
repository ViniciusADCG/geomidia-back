import asyncio
import sys
import unittest
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import AsyncSession

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.api.routes.application_forms import update_application_form
from app.db.models import ApplicationForm, MediaAsset, User
from app.main import app
from app.schemas import ApplicationFormRead, ApplicationFormUpdate


def linked_form(company_cnpj: str | None = "11222333000144") -> tuple[ApplicationForm, MediaAsset]:
    now = datetime.now(UTC)
    asset = MediaAsset(
        id=uuid.uuid4(),
        process_code="PROC-2026-999",
        media_type="outdoor",
        address="Avenida Afonso Pena, 1000",
        district="Centro",
        latitude=-20.46,
        longitude=-54.61,
        area_m2=12,
        bottom_height_m=4,
        radius_meters=80,
        status="novos processos",
        contact_name="Empresa Teste",
        contact_email="requerente@example.com",
        created_at=now,
        updated_at=now,
    )
    application_form = ApplicationForm(
        id=uuid.uuid4(),
        asset_id=asset.id,
        asset=asset,
        company_responsible="Empresa Teste",
        company_cnpj=company_cnpj,
        municipal_registration="12345",
        property_registration="12345678901",
        latitude=-20.46,
        longitude=-54.61,
        street="Avenida Afonso Pena",
        number="1000",
        district="Centro",
        postal_code="79002-000",
        media_type="outdoor",
        area_m2=12,
        bottom_height_m=4,
        number_of_faces="Duas",
        requester_email="requerente@example.com",
        attachment_links=None,
        attachments=[],
        created_at=now,
        updated_at=now,
    )
    return application_form, asset


def update_context(application_form: ApplicationForm):
    session = MagicMock(spec=AsyncSession)
    session.get = AsyncMock(return_value=application_form)
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    request = SimpleNamespace(state=SimpleNamespace(request_id="request-123"))
    user = User(
        id=uuid.uuid4(),
        username="analista",
        full_name="Analista",
        password_hash="unused",
        role="analyst",
    )
    return session, request, user


class ApplicationFormPatchTests(unittest.TestCase):
    def test_patch_persists_company_cnpj_returns_it_and_preserves_asset_link(self):
        application_form, asset = linked_form()
        original_asset_id = application_form.asset_id
        session, request, user = update_context(application_form)
        payload = ApplicationFormUpdate(company_cnpj="44555666000177")

        with (
            patch(
                "app.api.routes.application_forms.active_rule_for_type",
                new=AsyncMock(return_value=object()),
            ),
            patch("app.api.routes.application_forms.calculate_rule_radius", return_value=80),
        ):
            result = asyncio.run(update_application_form(application_form.id, payload, request, session, user))

        serialized = ApplicationFormRead.model_validate(result)
        self.assertEqual(application_form.company_cnpj, "44555666000177")
        self.assertEqual(serialized.company_cnpj, "44555666000177")
        self.assertEqual(application_form.asset_id, original_asset_id)
        self.assertIs(application_form.asset, asset)
        session.commit.assert_awaited_once()
        session.refresh.assert_awaited_once_with(application_form)

    def test_patch_with_null_clears_company_cnpj_and_preserves_asset_link(self):
        application_form, asset = linked_form()
        original_asset_id = application_form.asset_id
        session, request, user = update_context(application_form)
        payload = ApplicationFormUpdate(company_cnpj=None)

        with (
            patch(
                "app.api.routes.application_forms.active_rule_for_type",
                new=AsyncMock(return_value=object()),
            ),
            patch("app.api.routes.application_forms.calculate_rule_radius", return_value=80),
        ):
            result = asyncio.run(update_application_form(application_form.id, payload, request, session, user))

        serialized = ApplicationFormRead.model_validate(result)
        self.assertIsNone(application_form.company_cnpj)
        self.assertIsNone(serialized.company_cnpj)
        self.assertEqual(application_form.asset_id, original_asset_id)
        self.assertIs(application_form.asset, asset)
        session.commit.assert_awaited_once()


class ApplicationFormRouteTests(unittest.TestCase):
    def test_internal_creation_is_absent_and_public_submission_posts_remain(self):
        post_paths = {
            route.path
            for route in app.routes
            if "POST" in getattr(route, "methods", set())
        }

        self.assertNotIn("/api/application-forms", post_paths)
        self.assertIn("/api/public/solicitacoes/veiculos-divulgacao/iniciar", post_paths)
        self.assertIn(
            "/api/public/solicitacoes/veiculos-divulgacao/{draft_id}/finalizar",
            post_paths,
        )


if __name__ == "__main__":
    unittest.main()
