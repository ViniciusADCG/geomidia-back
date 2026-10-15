import asyncio
import builtins
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from test_application_forms import linked_form, update_context

from app.api.routes.application_forms import update_linked_application_form
from app.core.security import get_current_user
from app.db.models import ApplicationForm, MediaAsset
from app.main import app
from app.schemas import LinkedApplicationFormUpdate


def context(form: ApplicationForm, asset: MediaAsset | None = None):
    session, request, user = update_context(form)
    session.get = AsyncMock(side_effect=[form, asset if asset is not None else form.asset])
    session.flush = AsyncMock()
    session.rollback = AsyncMock()
    session.execute = AsyncMock()
    return session, request, user


def run_update(form: ApplicationForm, payload: LinkedApplicationFormUpdate, session, request, user):
    return asyncio.run(update_linked_application_form(form.id, payload, request, session, user))


class LinkedApplicationFormTests(unittest.TestCase):
    def test_conflicting_approval_requires_confirmation_of_current_conflicts(self):
        conflicts = [{
            "conflicting_asset_id": str(uuid.uuid4()),
            "process_code": "PROC-2026-100",
            "media_type": "outdoor",
            "distance_meters": 40.0,
            "minimum_distance_meters": 80,
        }]
        analysis = {"has_conflict": True, "message": "Ponto dentro do raio.", "conflicts": conflicts}

        form, asset = linked_form()
        asset.status = "análise"
        session, request, user = context(form, asset)
        with patch("app.api.routes.application_forms.analyze_asset", new=AsyncMock(return_value=analysis)):
            with self.assertRaises(HTTPException) as first_attempt:
                run_update(form, LinkedApplicationFormUpdate(asset={"status": "aprovado"}), session, request, user)
            self.assertTrue(first_attempt.exception.detail["confirmation_required"])
            self.assertEqual(first_attempt.exception.detail["conflicts"], conflicts)
            session.commit.assert_not_awaited()

        form, asset = linked_form()
        asset.status = "análise"
        session, request, user = context(form, asset)
        with patch("app.api.routes.application_forms.analyze_asset", new=AsyncMock(return_value=analysis)):
            with self.assertRaises(HTTPException) as stale_confirmation:
                run_update(form, LinkedApplicationFormUpdate(asset={
                    "status": "aprovado",
                    "confirm_conflict_approval": True,
                    "confirmed_conflicts": [{**conflicts[0], "distance_meters": 41.0}],
                }), session, request, user)
            self.assertEqual(stale_confirmation.exception.status_code, 409)
            session.commit.assert_not_awaited()

    def test_confirmed_conflicting_approval_is_audited(self):
        form, asset = linked_form()
        asset.status = "análise"
        session, request, user = context(form, asset)
        session.flush = AsyncMock()
        conflicts = [{
            "conflicting_asset_id": str(uuid.uuid4()),
            "process_code": "PROC-2026-100",
            "media_type": "outdoor",
            "distance_meters": 40.0,
            "minimum_distance_meters": 80,
        }]
        analysis = {"has_conflict": True, "message": "Ponto dentro do raio.", "conflicts": conflicts}

        with patch("app.api.routes.application_forms.analyze_asset", new=AsyncMock(return_value=analysis)):
            result = run_update(form, LinkedApplicationFormUpdate(asset={
                "status": "aprovado",
                "confirm_conflict_approval": True,
                "confirmed_conflicts": conflicts,
            }), session, request, user)

        self.assertEqual(result.asset.status.value, "aprovado")
        self.assertEqual(session.add.call_args.args[0].changes["conflict_approval"], {
            "confirmed": True, "conflicts": conflicts,
        })
        session.commit.assert_awaited_once()

    def test_complete_update_commits_both_and_returns_them(self):
        form, asset = linked_form()
        session, request, user = context(form)
        payload = LinkedApplicationFormUpdate(
            form={"company_responsible": "Empresa Nova", "street": "Rua Nova", "number": "25"},
            asset={"official_process_code": " 12345/2026 ", "width_m": 9},
        )

        result = run_update(form, payload, session, request, user)

        self.assertEqual(result.form.company_responsible, "Empresa Nova")
        self.assertEqual(result.asset.address, "Rua Nova, 25")
        self.assertEqual(result.asset.official_process_code, "12345/2026")
        self.assertEqual(result.asset.width_m, 9)
        self.assertEqual(form.asset_id, asset.id)
        session.flush.assert_awaited_once()
        session.commit.assert_awaited_once()
        session.rollback.assert_not_awaited()
        session.add.assert_called_once()

    def test_missing_form_or_asset_never_commits(self):
        form, _ = linked_form()
        session, request, user = context(form)
        session.get = AsyncMock(return_value=None)
        with self.assertRaises(HTTPException) as missing_form:
            run_update(form, LinkedApplicationFormUpdate(), session, request, user)
        self.assertEqual(missing_form.exception.status_code, 404)
        session.commit.assert_not_awaited()

        session, request, user = context(form)
        session.get = AsyncMock(side_effect=[form, None])
        with self.assertRaises(HTTPException) as missing_asset:
            run_update(form, LinkedApplicationFormUpdate(), session, request, user)
        self.assertEqual(missing_asset.exception.status_code, 409)
        session.commit.assert_not_awaited()
        session.rollback.assert_awaited_once()

    def test_validation_fails_before_mutation_or_commit(self):
        form, asset = linked_form()
        session, request, user = context(form)
        payload = LinkedApplicationFormUpdate(form={"latitude": -21.0})

        with self.assertRaises(HTTPException) as invalid:
            run_update(form, payload, session, request, user)

        self.assertEqual(invalid.exception.status_code, 422)
        self.assertEqual(form.latitude, -20.46)
        self.assertEqual(asset.latitude, -20.46)
        session.flush.assert_not_awaited()
        session.commit.assert_not_awaited()
        session.rollback.assert_awaited_once()

    def test_asset_update_error_rolls_back_form_change(self):
        form, asset = linked_form()
        session, request, user = context(form)
        original_company = form.company_responsible
        original_width = asset.width_m

        async def rollback():
            form.company_responsible = original_company
            asset.width_m = original_width

        session.rollback.side_effect = rollback
        original_setattr = builtins.setattr

        def failing_setattr(obj, name, value):
            if obj is asset and name == "width_m":
                raise RuntimeError("falha no ativo")
            original_setattr(obj, name, value)

        with patch("app.api.routes.application_forms.setattr", side_effect=failing_setattr, create=True):
            with self.assertRaisesRegex(RuntimeError, "falha no ativo"):
                run_update(form, LinkedApplicationFormUpdate(
                    form={"company_responsible": "Empresa Nova"}, asset={"width_m": 9},
                ), session, request, user)

        self.assertEqual(form.company_responsible, original_company)
        self.assertEqual(asset.width_m, original_width)
        session.commit.assert_not_awaited()
        session.rollback.assert_awaited_once()

    def test_form_update_error_keeps_asset_unchanged(self):
        form, asset = linked_form()
        session, request, user = context(form)
        original_setattr = builtins.setattr

        def failing_setattr(obj, name, value):
            if obj is form and name == "company_responsible":
                raise RuntimeError("falha no formulario")
            original_setattr(obj, name, value)

        with patch("app.api.routes.application_forms.setattr", side_effect=failing_setattr, create=True):
            with self.assertRaisesRegex(RuntimeError, "falha no formulario"):
                run_update(form, LinkedApplicationFormUpdate(
                    form={"company_responsible": "Empresa Nova"}, asset={"width_m": 9},
                ), session, request, user)

        self.assertEqual(form.company_responsible, "Empresa Teste")
        self.assertIsNone(asset.width_m)
        session.commit.assert_not_awaited()
        session.rollback.assert_awaited_once()

    def test_inconsistent_asset_id_is_rejected(self):
        form, asset = linked_form()
        form.asset_id = uuid.uuid4()
        session, request, user = context(form, asset)

        with self.assertRaises(HTTPException) as invalid:
            run_update(form, LinkedApplicationFormUpdate(), session, request, user)

        self.assertEqual(invalid.exception.status_code, 409)
        session.commit.assert_not_awaited()
        session.rollback.assert_awaited_once()

    def test_optional_nulls_are_accepted(self):
        form, asset = linked_form()
        session, request, user = context(form)
        result = run_update(form, LinkedApplicationFormUpdate(
            form={"company_cnpj": None, "number_of_faces": None, "expiration_date": None},
            asset={"width_m": None, "top_height_m": None, "official_process_code": None},
        ), session, request, user)

        self.assertIsNone(result.form.company_cnpj)
        self.assertIsNone(result.form.number_of_faces)
        self.assertIsNone(result.asset.width_m)
        self.assertIsNone(result.asset.official_process_code)
        self.assertIsNone(asset.expiration_date)
        session.commit.assert_awaited_once()

    def test_shared_fields_stay_synchronized(self):
        form, asset = linked_form()
        session, request, user = context(form)
        run_update(form, LinkedApplicationFormUpdate(form={
            "district": "Jardim dos Estados", "latitude": -20.47, "longitude": -54.62,
            "media_type": "front light", "street": "Rua Nova", "number": "30",
            "requester_email": "novo@example.com", "expiration_date": "2027-01-01",
        }), session, request, user)

        self.assertEqual(form.district, asset.district)
        self.assertEqual(form.latitude, asset.latitude)
        self.assertEqual(form.longitude, asset.longitude)
        self.assertEqual(form.media_type, asset.media_type)
        self.assertEqual(asset.address, "Rua Nova, 30")
        self.assertEqual(asset.contact_email, form.requester_email)
        self.assertEqual(asset.expiration_date.isoformat(), "2027-01-01")
        session.commit.assert_awaited_once()

    def test_fields_not_sent_are_preserved(self):
        form, asset = linked_form()
        asset.official_process_code = "ANTIGO/2026"
        asset.width_m = 7
        session, request, user = context(form)
        result = run_update(form, LinkedApplicationFormUpdate(form={"company_cnpj": None}), session, request, user)

        self.assertEqual(result.asset.official_process_code, "ANTIGO/2026")
        self.assertEqual(result.asset.width_m, 7)
        self.assertEqual(result.form.street, "Avenida Afonso Pena")
        self.assertEqual(form.area_m2, 12)
        self.assertEqual(asset.area_m2, 12)
        self.assertEqual(asset.radius_meters, 80)
        self.assertEqual(form.asset_id, asset.id)

    def test_protected_and_historical_fields_are_not_accepted(self):
        with self.assertRaises(ValueError):
            LinkedApplicationFormUpdate(form={"asset_id": str(uuid.uuid4())})
        with self.assertRaises(ValueError):
            LinkedApplicationFormUpdate(form={"area_m2": 99})
        with self.assertRaises(ValueError):
            LinkedApplicationFormUpdate(asset={"process_code": "ALTERADO"})

    def test_flush_failure_rolls_back_both_entities(self):
        form, asset = linked_form()
        session, request, user = context(form)
        session.flush.side_effect = RuntimeError("falha ao gravar")

        with self.assertRaisesRegex(RuntimeError, "falha ao gravar"):
            run_update(form, LinkedApplicationFormUpdate(
                form={"company_responsible": "Empresa Nova"}, asset={"width_m": 9},
            ), session, request, user)

        session.commit.assert_not_awaited()
        session.rollback.assert_awaited_once()


class LinkedApplicationFormPermissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        app.dependency_overrides.clear()

    async def test_viewer_cannot_call_linked_update(self):
        async def viewer():
            return SimpleNamespace(role="viewer")

        app.dependency_overrides[get_current_user] = viewer
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.patch(f"/api/application-forms/{uuid.uuid4()}/linked", json={"form": {}, "asset": {}})
        self.assertEqual(response.status_code, 403)
