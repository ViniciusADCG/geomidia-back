import sys
import unittest
import uuid
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.core.security import create_access_token, decode_access_token, hash_password, verify_password
from app.db.models import User


class AuthTests(unittest.TestCase):
    def test_password_is_hashed_and_verified(self):
        digest = hash_password("uma-senha-forte-123")
        self.assertNotIn("uma-senha-forte-123", digest)
        self.assertTrue(verify_password("uma-senha-forte-123", digest))
        self.assertFalse(verify_password("senha-incorreta", digest))

    def test_access_token_contains_user_identity(self):
        user_id = uuid.uuid4()
        user = User(
            id=user_id,
            username="admin",
            full_name="Administrador",
            password_hash="unused",
            role="admin",
        )
        token, expires_at = create_access_token(user)
        payload = decode_access_token(token)

        self.assertEqual(payload["sub"], str(user_id))
        self.assertEqual(payload["role"], "admin")
        self.assertGreater(expires_at.timestamp(), 0)


if __name__ == "__main__":
    unittest.main()
