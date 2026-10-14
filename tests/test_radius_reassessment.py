import asyncio
import unittest
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.media_assets import confirm_radius_reassessment, create_media_asset, reassessment_preview
from app.api.routes.media_rules import create_media_rule, update_media_rule
from app.core.security import get_current_user
from app.db.models import MediaAsset, MediaRule, User
from app.db.session import get_session
from app.main import app
from app.schemas import MediaAssetCreate, MediaRuleCreate, MediaRuleUpdate, RadiusReassessmentConfirm


def existing_rule() -> MediaRule:
    now = datetime.now(UTC)
    return MediaRule(
        id=uuid.uuid4(), media_type="painel de led", name="Painel pequeno",
        base_radius_meters=250, area_threshold_m2=5,
        radius_above_threshold_meters=1000, is_active=True,
        created_at=now, updated_at=now,
    )


def existing_asset() -> MediaAsset:
    return MediaAsset(
        id=uuid.uuid4(), process_code="PROC-2026-123", media_type="painel de led",
        address="Rua Teste, 10", district="Centro", latitude=-20.46, longitude=-54.61,
        area_m2=6, radius_meters=250, status="analise", created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


class HistoricalRadiusTests(unittest.TestCase):
    def test_reassessment_reason_is_required(self):
        with self.assertRaises(ValidationError):
            RadiusReassessmentConfirm(
                reason="   ", expected_registered_radius_meters=250,
                expected_rule_updated_at=datetime.now(UTC),
            )

    def test_new_manual_asset_uses_active_rule_and_keeps_coordinates(self):
        rule = existing_rule()
        session = MagicMock(spec=AsyncSession)
        session.flush = AsyncMock()
        session.commit = AsyncMock()
        session.refresh = AsyncMock()
        payload = MediaAssetCreate(
            media_type="painel de led", address="Rua Teste, 10", district="Centro",
            latitude=-20.46, longitude=-54.61, area_m2=6,
        )
        request = SimpleNamespace(state=SimpleNamespace(request_id="request-123"))
        user = User(id=uuid.uuid4(), username="admin", full_name="Admin", password_hash="x", role="admin")
        with (
            patch("app.api.routes.media_assets.active_rule_for_type", new=AsyncMock(return_value=rule)),
            patch("app.api.routes.media_assets.next_process_code", new=AsyncMock(return_value="PROC-2026-124")),
            patch("app.api.routes.media_assets.asset_for_user", side_effect=lambda asset, _: asset),
        ):
            result = asyncio.run(create_media_asset(payload, request, session, user))
        self.assertEqual(result.radius_meters, 1000)
        self.assertEqual((result.latitude, result.longitude), (-20.46, -54.61))

    def test_rule_edits_never_scan_or_recalculate_existing_assets(self):
        for changes in (
            {"description": "Nova descricao"},
            {"base_radius_meters": 300},
            {"area_threshold_m2": 8},
            {"is_active": False},
            {"is_active": True},
        ):
            with self.subTest(changes=changes):
                rule = existing_rule()
                session = MagicMock(spec=AsyncSession)
                session.get = AsyncMock(return_value=rule)
                session.commit = AsyncMock()
                session.refresh = AsyncMock()
                session.scalars = AsyncMock()
                asyncio.run(update_media_rule(rule.id, MediaRuleUpdate(**changes), session, None))
                session.scalars.assert_not_awaited()
                session.commit.assert_awaited_once()

    def test_new_rule_does_not_scan_existing_assets(self):
        session = MagicMock(spec=AsyncSession)
        session.flush = AsyncMock()
        session.commit = AsyncMock()
        session.refresh = AsyncMock()
        session.scalars = AsyncMock()
        payload = MediaRuleCreate(
            media_type="painel de led", name="Painel pequeno", base_radius_meters=250,
            area_threshold_m2=5, radius_above_threshold_meters=1000,
        )
        asyncio.run(create_media_rule(payload, session, None))
        session.scalars.assert_not_awaited()

    def test_one_asset_is_reassessed_and_audited_without_moving_it(self):
        asset = existing_asset()
        other = existing_asset()
        asset.radius_meters = 1000
        rule = existing_rule()
        rule.area_threshold_m2 = 8
        before_location = (asset.latitude, asset.longitude)
        session = MagicMock(spec=AsyncSession)
        session.scalar = AsyncMock(side_effect=[asset, rule])
        session.commit = AsyncMock()
        session.refresh = AsyncMock()
        request = SimpleNamespace(state=SimpleNamespace(request_id="request-123"))
        user = User(id=uuid.uuid4(), username="admin", full_name="Admin", password_hash="x", role="admin")
        payload = RadiusReassessmentConfirm(
            reason="Area real conferida no processo", expected_registered_radius_meters=1000,
            expected_rule_updated_at=rule.updated_at,
        )
        with patch("app.api.routes.media_assets.asset_for_user", side_effect=lambda item, _: item):
            result = asyncio.run(confirm_radius_reassessment(asset.id, payload, request, session, user))
        self.assertIs(result, asset)
        self.assertEqual(asset.radius_meters, 250)
        self.assertEqual(other.radius_meters, 250)
        self.assertEqual((asset.latitude, asset.longitude), before_location)
        log = session.add.call_args.args[0]
        self.assertEqual(log.asset_id, asset.id)
        self.assertEqual(log.actor_user_id, user.id)
        self.assertEqual(log.changes["radius_meters"], {"before": 1000, "after": 250})
        self.assertEqual(log.changes["reason"], payload.reason)
        self.assertEqual(log.changes["rule"]["id"], str(rule.id))

    def test_classification_only_is_blocked_without_inventing_area(self):
        asset = existing_asset()
        asset.area_m2 = None
        asset.area_rule_classification = "above_limit"
        rule = existing_rule()
        preview = reassessment_preview(asset, rule)
        self.assertFalse(preview.can_reassess)
        self.assertIsNone(preview.current_radius_meters)
        self.assertIsNone(asset.area_m2)
        session = MagicMock(spec=AsyncSession)
        session.scalar = AsyncMock(side_effect=[asset, rule])
        session.commit = AsyncMock()
        request = SimpleNamespace(state=SimpleNamespace(request_id="request-123"))
        payload = RadiusReassessmentConfirm(
            reason="Revisao solicitada pelo administrador", expected_registered_radius_meters=250,
            expected_rule_updated_at=rule.updated_at,
        )
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(confirm_radius_reassessment(asset.id, payload, request, session, None))
        self.assertEqual(caught.exception.status_code, 422)
        session.commit.assert_not_awaited()
        self.assertEqual(asset.radius_meters, 250)

    def test_stale_preview_requires_new_confirmation(self):
        asset = existing_asset()
        rule = existing_rule()
        session = MagicMock(spec=AsyncSession)
        session.scalar = AsyncMock(side_effect=[asset, rule])
        session.commit = AsyncMock()
        request = SimpleNamespace(state=SimpleNamespace(request_id="request-123"))
        payload = RadiusReassessmentConfirm(
            reason="Revisao solicitada pelo administrador", expected_registered_radius_meters=1000,
            expected_rule_updated_at=rule.updated_at,
        )
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(confirm_radius_reassessment(asset.id, payload, request, session, None))
        self.assertEqual(caught.exception.status_code, 409)
        session.commit.assert_not_awaited()


class ReassessmentPermissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_admin_can_preview_or_confirm(self):
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(role="analyst")

        async def session():
            yield object()

        app.dependency_overrides[get_session] = session
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                path = f"/api/media-assets/{uuid.uuid4()}/radius-reassessment"
                self.assertEqual((await client.get(path)).status_code, 403)
                self.assertEqual((await client.post(path, json={
                    "reason": "Motivo detalhado", "expected_registered_radius_meters": 250,
                    "expected_rule_updated_at": datetime.now(UTC).isoformat(),
                })).status_code, 403)
        finally:
            app.dependency_overrides.clear()
