import asyncio
import sys
import unittest
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.api.routes.media_assets import asset_for_user
from app.core.security import get_current_user, require_roles
from app.db.models import MediaAsset, User
from app.db.session import get_session
from app.main import app


def user_with_role(role: str) -> User:
    return User(username=f"user-{role}", full_name=role, password_hash="unused", role=role)


class PermissionTests(unittest.TestCase):
    def test_admin_dependency_accepts_admin(self):
        dependency = require_roles("admin")
        user = user_with_role("admin")
        self.assertIs(asyncio.run(dependency(user)), user)

    def test_admin_dependency_rejects_viewer(self):
        dependency = require_roles("admin")
        with self.assertRaises(HTTPException) as context:
            asyncio.run(dependency(user_with_role("viewer")))
        self.assertEqual(context.exception.status_code, 403)

    def test_viewer_cannot_read_contact_data(self):
        now = datetime.now(UTC)
        asset = MediaAsset(
            id=uuid.uuid4(),
            process_code="PROC-2026-999",
            media_type="outdoor",
            address="Av. Teste, 100",
            district="Centro",
            latitude=-20.46,
            longitude=-54.61,
            area_m2=27,
            bottom_height_m=5,
            radius_meters=80,
            status="análise",
            contact_name="Contato Privado",
            contact_email="privado@example.com",
            created_at=now,
            updated_at=now,
        )
        result = asset_for_user(asset, user_with_role("viewer"))
        self.assertIsNone(result.contact_name)
        self.assertIsNone(result.contact_email)


class DeleteMediaAssetPermissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        app.dependency_overrides.clear()

    async def test_analyst_can_delete_asset(self):
        now = datetime.now(UTC)
        asset = MediaAsset(
            id=uuid.uuid4(),
            process_code="PROC-2026-999",
            media_type="outdoor",
            address="Av. Teste, 100",
            district="Centro",
            latitude=-20.46,
            longitude=-54.61,
            area_m2=27,
            bottom_height_m=5,
            radius_meters=80,
            status="análise",
            created_at=now,
            updated_at=now,
        )
        session = SimpleNamespace(
            get=AsyncMock(return_value=asset),
            add=Mock(),
            delete=AsyncMock(),
            commit=AsyncMock(),
        )

        async def analyst():
            return SimpleNamespace(id=uuid.uuid4(), role="analyst")

        async def fake_session():
            yield session

        app.dependency_overrides[get_current_user] = analyst
        app.dependency_overrides[get_session] = fake_session
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.request("DELETE", f"/api/media-assets/{asset.id}",
                                            json={"reason": "Cadastro duplicado confirmado."})

        self.assertEqual(response.status_code, 204)
        session.get.assert_awaited_once()
        session.delete.assert_awaited_once_with(asset)
        session.commit.assert_awaited_once()
        session.add.assert_called_once()
        self.assertEqual(session.add.call_args.args[0].changes["reason"], "Cadastro duplicado confirmado.")

    async def test_deletion_requires_nonempty_reason(self):
        session = SimpleNamespace(get=AsyncMock(), delete=AsyncMock())

        async def analyst():
            return SimpleNamespace(id=uuid.uuid4(), role="analyst")

        async def fake_session():
            yield session

        app.dependency_overrides[get_current_user] = analyst
        app.dependency_overrides[get_session] = fake_session
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for payload in ({}, {"reason": "   "}):
                response = await client.request("DELETE", f"/api/media-assets/{uuid.uuid4()}", json=payload)
                self.assertEqual(response.status_code, 422)
        session.get.assert_not_awaited()

    async def test_form_deletion_also_requires_reason(self):
        session = SimpleNamespace(get=AsyncMock())

        async def admin():
            return SimpleNamespace(id=uuid.uuid4(), role="admin")

        async def fake_session():
            yield session

        app.dependency_overrides[get_current_user] = admin
        app.dependency_overrides[get_session] = fake_session
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.request("DELETE", f"/api/application-forms/{uuid.uuid4()}",
                                            json={"reason": "  "})
        self.assertEqual(response.status_code, 422)
        session.get.assert_not_awaited()

    async def test_viewer_cannot_reach_delete_route(self):
        session = SimpleNamespace(get=AsyncMock(return_value=None))

        async def viewer():
            return SimpleNamespace(role="viewer")

        async def fake_session():
            yield session

        app.dependency_overrides[get_current_user] = viewer
        app.dependency_overrides[get_session] = fake_session
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.delete(f"/api/media-assets/{uuid.uuid4()}")

        self.assertEqual(response.status_code, 403)
        session.get.assert_not_awaited()
