import sys
import unittest
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.core.config import Settings


class DatabaseSettingsTests(unittest.TestCase):
    def test_normalizes_supabase_url_for_asyncpg(self):
        settings = Settings(
            database_url=(
                "postgresql://postgres.ref:secret@pooler.example.com:6543/postgres"
                "?sslmode=require&channel_binding=require"
            )
        )

        self.assertEqual(
            settings.database_url,
            "postgresql+asyncpg://postgres.ref:secret@pooler.example.com:6543/postgres?ssl=require",
        )
        self.assertTrue(settings.uses_transaction_pooler)

    def test_prefers_direct_url_for_migrations(self):
        settings = Settings(
            database_url="postgresql://postgres.ref:secret@pooler.example.com:6543/postgres",
            database_direct_url="postgresql://postgres:secret@db.example.com:5432/postgres",
        )

        self.assertEqual(
            settings.migration_database_url,
            "postgresql+asyncpg://postgres:secret@db.example.com:5432/postgres",
        )


if __name__ == "__main__":
    unittest.main()
