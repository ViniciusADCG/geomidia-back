import asyncio
import sys
import unittest
import uuid
from datetime import UTC, datetime
from pathlib import Path

from fastapi import HTTPException

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.api.routes.media_assets import asset_for_user
from app.core.security import require_roles
from app.db.models import MediaAsset, User


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
