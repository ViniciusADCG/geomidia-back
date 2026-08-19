import sys
import unittest
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.db.models import ActivityLog, ApplicationForm, Base


class ModelSchemaTests(unittest.TestCase):
    def test_all_application_tables_use_public_schema(self):
        self.assertTrue(Base.metadata.tables)
        self.assertTrue(all(table.schema == "public" for table in Base.metadata.tables.values()))

    def test_foreign_keys_are_schema_qualified(self):
        application_asset_fk = next(iter(ApplicationForm.__table__.c.asset_id.foreign_keys))
        activity_asset_fk = next(iter(ActivityLog.__table__.c.asset_id.foreign_keys))
        activity_actor_fk = next(iter(ActivityLog.__table__.c.actor_user_id.foreign_keys))

        self.assertEqual(application_asset_fk.target_fullname, "public.media_assets.id")
        self.assertEqual(activity_asset_fk.target_fullname, "public.media_assets.id")
        self.assertEqual(activity_actor_fk.target_fullname, "public.users.id")


if __name__ == "__main__":
    unittest.main()
