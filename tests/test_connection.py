import asyncio
import sys
import unittest
from pathlib import Path

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.db.connection import engine_options_for


class EngineOptionsTests(unittest.TestCase):
    def test_transaction_pooler_disables_prepared_statements(self):
        url = "postgresql+psycopg://user:secret@pooler.example.com:6543/postgres"
        options = engine_options_for(url)

        self.assertIs(options["poolclass"], NullPool)
        self.assertEqual(options["connect_args"], {"prepare_threshold": None})

        engine = create_async_engine(url, **options)
        try:
            self.assertEqual(engine.dialect.driver, "psycopg")
            self.assertIsInstance(engine.sync_engine.pool, NullPool)
        finally:
            asyncio.run(engine.dispose())

    def test_direct_connection_keeps_driver_defaults(self):
        options = engine_options_for("postgresql+psycopg://user:secret@db.example.com:5432/postgres")

        self.assertNotIn("connect_args", options)
        self.assertNotIn("poolclass", options)
