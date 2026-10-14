import asyncio
import os
import unittest
from datetime import UTC, datetime
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

from app.api.routes.public_submissions import enforce_public_draft_limit, finalize_public_submission
from app.core.config import Settings
from app.db.models import (
    ActivityLog,
    ApplicationForm,
    ApplicationFormAttachment,
    MediaAsset,
    PublicProcessCounter,
    PublicSubmissionDraft,
)
from app.schemas import PublicSubmissionFinalize
from app.services.storage import ObjectMetadata
from tests.test_public_submissions import valid_manifest, valid_public_payload


class PublicSubmissionIdempotencyPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_initiations_share_the_database_limit(self):
        fingerprint = sha256(uuid4().bytes).hexdigest()
        new_draft_id = uuid4()
        settings = Settings(public_submission_rate_limit=1)
        first_checked = asyncio.Event()
        release_first = asyncio.Event()

        async def first():
            async with self.sessions() as session:
                await enforce_public_draft_limit(session, fingerprint, settings)
                session.add(PublicSubmissionDraft(
                    id=new_draft_id, token_hash="a" * 64, client_fingerprint=fingerprint,
                    payload={}, attachments=[], created_at=datetime.now(UTC),
                ))
                first_checked.set()
                await release_first.wait()
                await session.commit()

        async def second():
            await first_checked.wait()
            async with self.sessions() as session:
                with self.assertRaises(HTTPException) as context:
                    await enforce_public_draft_limit(session, fingerprint, settings)
                self.assertEqual(context.exception.status_code, 429)

        first_task = asyncio.create_task(first())
        second_task = asyncio.create_task(second())
        try:
            await first_checked.wait()
            await asyncio.sleep(0.05)
            release_first.set()
            await asyncio.gather(first_task, second_task)
        finally:
            release_first.set()
            async with self.sessions() as session:
                await session.execute(delete(PublicSubmissionDraft).where(PublicSubmissionDraft.id == new_draft_id))
                await session.commit()

    async def asyncSetUp(self):
        database_url = os.getenv("DATABASE_URL", "")
        if not database_url.rsplit("/", 1)[-1].split("?", 1)[0].endswith("_test"):
            self.skipTest("Idempotency integration requires an explicit *_test DATABASE_URL")
        self.engine = create_async_engine(database_url)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.draft_id = uuid4()
        self.token = "test-token-" + uuid4().hex
        records = [
            {**item.model_dump(), "object_path": f"public-submissions/{self.draft_id}/{item.category}/{item.client_id}.pdf"}
            for item in valid_manifest()
        ]
        async with self.sessions() as session:
            session.add(PublicSubmissionDraft(
                id=self.draft_id, token_hash=sha256(self.token.encode()).hexdigest(),
                client_fingerprint="test-" + uuid4().hex,
                payload=valid_public_payload().model_dump(mode="json"), attachments=records,
                created_at=datetime.now(UTC),
            ))
            await session.commit()
        storage = SimpleNamespace(
            get_object_metadata=AsyncMock(return_value=ObjectMetadata(1024, "application/pdf")),
            get_object_signature=AsyncMock(return_value=b"%PDF-1.7"),
        )
        rule = SimpleNamespace(base_radius_meters=80, area_threshold_m2=None, radius_above_threshold_meters=None)
        self.storage_patch = patch("app.api.routes.public_submissions.storage_or_503", return_value=storage)
        self.rule_patch = patch("app.api.routes.public_submissions.active_rule_for_type", new_callable=AsyncMock,
                                return_value=rule)
        self.storage_patch.start()
        self.rule_patch.start()

    async def asyncTearDown(self):
        if not hasattr(self, "engine"):
            return
        self.rule_patch.stop()
        self.storage_patch.stop()
        async with self.sessions() as session:
            draft = await session.get(PublicSubmissionDraft, self.draft_id)
            if draft and draft.process_code:
                asset_id = await session.scalar(select(MediaAsset.id).where(MediaAsset.process_code == draft.process_code))
                if asset_id:
                    await session.execute(delete(ActivityLog).where(ActivityLog.asset_id == asset_id))
                    await session.execute(delete(MediaAsset).where(MediaAsset.id == asset_id))
            await session.execute(delete(PublicSubmissionDraft).where(PublicSubmissionDraft.id == self.draft_id))
            await session.commit()
        await self.engine.dispose()

    async def finalize(self, *, token=None, fail_commit=False):
        request = Request({
            "type": "http", "headers": [(b"origin", b"http://localhost:5173")],
            "client": ("127.0.0.1", 1234),
        })
        request.state.request_id = str(uuid4())
        async with self.sessions() as session:
            if fail_commit:
                with patch.object(session, "commit", side_effect=RuntimeError("commit interrupted")):
                    with self.assertRaisesRegex(RuntimeError, "commit interrupted"):
                        await finalize_public_submission(
                            self.draft_id, PublicSubmissionFinalize(token=token or self.token), request, session,
                        )
                await session.rollback()
                return None
            return await finalize_public_submission(
                self.draft_id, PublicSubmissionFinalize(token=token or self.token), request, session,
            )

    async def counts(self, code):
        async with self.sessions() as session:
            assets = await session.scalar(select(func.count()).select_from(MediaAsset).where(MediaAsset.process_code == code))
            forms = await session.scalar(
                select(func.count()).select_from(ApplicationForm)
                .join(MediaAsset, ApplicationForm.asset_id == MediaAsset.id)
                .where(MediaAsset.process_code == code)
            )
            attachments = await session.scalar(
                select(func.count()).select_from(ApplicationFormAttachment)
                .join(ApplicationForm, ApplicationFormAttachment.application_form_id == ApplicationForm.id)
                .join(MediaAsset, ApplicationForm.asset_id == MediaAsset.id)
                .where(MediaAsset.process_code == code)
            )
            return assets, forms, attachments

    async def test_retry_after_commit_reuses_everything(self):
        first = await self.finalize()
        async with self.sessions() as session:
            counter = await session.get(PublicProcessCounter, datetime.now(UTC).year)
            first_counter = counter.last_value
        second = await self.finalize()
        self.assertEqual(first.model_dump(), second.model_dump())
        self.assertEqual(await self.counts(first.protocolo), (1, 1, len(valid_manifest())))
        async with self.sessions() as session:
            self.assertEqual((await session.get(PublicProcessCounter, datetime.now(UTC).year)).last_value, first_counter)

    async def test_rollback_allows_retry_without_false_finalization(self):
        async with self.sessions() as session:
            previous_assets = await session.scalar(select(func.count()).select_from(MediaAsset))
        await self.finalize(fail_commit=True)
        async with self.sessions() as session:
            draft = await session.get(PublicSubmissionDraft, self.draft_id)
            self.assertIsNone(draft.finalized_at)
            self.assertIsNone(draft.process_code)
            self.assertEqual(await session.scalar(select(func.count()).select_from(MediaAsset)), previous_assets)
        result = await self.finalize()
        self.assertEqual(await self.counts(result.protocolo), (1, 1, len(valid_manifest())))

    async def test_concurrent_calls_create_one_result(self):
        first, second = await asyncio.gather(self.finalize(), self.finalize())
        self.assertEqual(first.model_dump(), second.model_dump())
        self.assertEqual(await self.counts(first.protocolo), (1, 1, len(valid_manifest())))
