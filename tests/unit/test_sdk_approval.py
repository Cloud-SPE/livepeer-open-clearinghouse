"""Tests for the SDK approval list + admin visibility surface.

Pattern follows tests/unit/test_operator_crud.py — in-memory SQLite via
aiosqlite, hand-rolled session fixture, no FastAPI app round-trip
(the runtime layer is a thin model->view shim).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from livepeer_open_clearinghouse.domains.accounts import repo as _accounts  # noqa: F401
from livepeer_open_clearinghouse.domains.accounts.repo import User
from livepeer_open_clearinghouse.domains.admin import service as admin_service
from livepeer_open_clearinghouse.domains.admin.repo import Operator
from livepeer_open_clearinghouse.domains.api_keys import repo as _api_keys  # noqa: F401
from livepeer_open_clearinghouse.domains.api_keys.repo import ApiKey
from livepeer_open_clearinghouse.domains.billing import repo as _billing  # noqa: F401
from livepeer_open_clearinghouse.domains.notifications import repo as _notif  # noqa: F401
from livepeer_open_clearinghouse.domains.payments import repo as _payments  # noqa: F401
from livepeer_open_clearinghouse.domains.sessions import repo as sessions_repo
from livepeer_open_clearinghouse.domains.usage import repo as _usage  # noqa: F401
from livepeer_open_clearinghouse.providers.db.base import Base


@pytest_asyncio.fixture()
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as s:
        yield s
    await engine.dispose()


@pytest_asyncio.fixture()
async def operator(session: AsyncSession) -> Operator:
    op = Operator(
        email="owner@example.com",
        name="Owner",
        token_hash="hash-not-checked",
        role="owner",
    )
    session.add(op)
    await session.flush()
    return op


@pytest.mark.unit
class TestParseSdkIdentity:
    def test_none(self) -> None:
        assert admin_service.parse_sdk_identity(None) is None

    def test_empty(self) -> None:
        assert admin_service.parse_sdk_identity("") is None

    def test_too_few_parts(self) -> None:
        assert admin_service.parse_sdk_identity("python/0.2.0") is None

    def test_too_many_parts(self) -> None:
        assert admin_service.parse_sdk_identity("python/0.2.0/abc1234/extra") is None

    def test_blank_part(self) -> None:
        assert admin_service.parse_sdk_identity("python//abc1234") is None

    def test_ok(self) -> None:
        assert admin_service.parse_sdk_identity("python/0.2.0/abc1234") == (
            "python",
            "0.2.0",
            "abc1234",
        )

    def test_strips_whitespace(self) -> None:
        assert admin_service.parse_sdk_identity("  rust/1.0.0/aaaa  ") == (
            "rust",
            "1.0.0",
            "aaaa",
        )


@pytest.mark.unit
async def test_create_list_update_delete(session: AsyncSession, operator: Operator) -> None:
    row = await admin_service.create_sdk_approval(
        session,
        acting_operator=operator,
        lang="python",
        version="0.2.0",
        git_sha7="abc1234",
        status="approved",
        notes="initial",
    )
    assert row.lang == "python"
    assert row.status == "approved"
    assert row.added_by_operator_id == operator.id

    listed = await admin_service.list_sdk_approvals(session)
    assert len(listed) == 1
    assert listed[0].id == row.id

    updated = await admin_service.update_sdk_approval(
        session,
        acting_operator=operator,
        approval_id=row.id,
        status="deprecated",
        notes="superseded by 0.3.0",
    )
    assert updated.status == "deprecated"
    assert updated.notes == "superseded by 0.3.0"

    await admin_service.delete_sdk_approval(session, acting_operator=operator, approval_id=row.id)
    listed_after = await admin_service.list_sdk_approvals(session)
    assert listed_after == []


@pytest.mark.unit
async def test_create_rejects_invalid_status(session: AsyncSession, operator: Operator) -> None:
    with pytest.raises(admin_service.InvalidSdkApprovalStatus):
        await admin_service.create_sdk_approval(
            session,
            acting_operator=operator,
            lang="go",
            version="0.2.0",
            git_sha7="def4567",
            status="bogus",
            notes=None,
        )


@pytest.mark.unit
async def test_create_rejects_duplicate_triple(session: AsyncSession, operator: Operator) -> None:
    await admin_service.create_sdk_approval(
        session,
        acting_operator=operator,
        lang="go",
        version="0.2.0",
        git_sha7="def4567",
        status="approved",
        notes=None,
    )
    with pytest.raises(admin_service.SdkApprovalAlreadyExists):
        await admin_service.create_sdk_approval(
            session,
            acting_operator=operator,
            lang="go",
            version="0.2.0",
            git_sha7="def4567",
            status="approved",
            notes=None,
        )


@pytest.mark.unit
async def test_update_unknown_id_raises(session: AsyncSession, operator: Operator) -> None:
    with pytest.raises(admin_service.SdkApprovalNotFound):
        await admin_service.update_sdk_approval(
            session,
            acting_operator=operator,
            approval_id=uuid.uuid4(),
            status="approved",
            notes=None,
        )


@pytest.mark.unit
async def test_evaluate_sdk_identity_buckets(session: AsyncSession, operator: Operator) -> None:
    await admin_service.create_sdk_approval(
        session,
        acting_operator=operator,
        lang="rust",
        version="0.2.0",
        git_sha7="aaaaaaa",
        status="approved",
        notes=None,
    )
    await admin_service.create_sdk_approval(
        session,
        acting_operator=operator,
        lang="rust",
        version="0.1.0",
        git_sha7="bbbbbbb",
        status="deprecated",
        notes="EOL",
    )
    await admin_service.create_sdk_approval(
        session,
        acting_operator=operator,
        lang="rust",
        version="0.0.1",
        git_sha7="ccccccc",
        status="blocked",
        notes="cheats",
    )

    assert (
        await admin_service.evaluate_sdk_identity(session, sdk_identity="rust/0.2.0/aaaaaaa")
        == "approved"
    )
    assert (
        await admin_service.evaluate_sdk_identity(session, sdk_identity="rust/0.1.0/bbbbbbb")
        == "deprecated"
    )
    assert (
        await admin_service.evaluate_sdk_identity(session, sdk_identity="rust/0.0.1/ccccccc")
        == "blocked"
    )
    assert (
        await admin_service.evaluate_sdk_identity(session, sdk_identity="rust/99.99.99/ddddddd")
        == "unknown"
    )
    assert await admin_service.evaluate_sdk_identity(session, sdk_identity=None) == "unknown"


@pytest.mark.unit
async def test_list_approved_sdk_manifest_excludes_blocked(
    session: AsyncSession, operator: Operator
) -> None:
    for status, sha in [("approved", "1111111"), ("deprecated", "2222222"), ("blocked", "3333333")]:
        await admin_service.create_sdk_approval(
            session,
            acting_operator=operator,
            lang="ts",
            version="0.2.0",
            git_sha7=sha,
            status=status,
            notes=None,
        )
    manifest = await admin_service.list_approved_sdk_manifest(session)
    statuses = {row.status for row in manifest}
    assert statuses == {"approved", "deprecated"}


def _make_session_row(*, user, api_key, sdk_identity: str | None) -> sessions_repo.PaymentSession:
    return sessions_repo.PaymentSession(
        user_id=user.id,
        api_key_id=api_key.id,
        work_id="wid-fake",
        capability="cap.x",
        offering="off.y",
        protocol="paid-session/v1",
        state="open",
        estimated_units=10,
        max_total_units=100,
        funded_value_wei=Decimal(1_000),
        sdk_identity=sdk_identity,
        opened_at=datetime(2026, 5, 24, 12, 0, tzinfo=UTC),
    )


@pytest.mark.unit
async def test_list_recent_sessions_attaches_sdk_status(
    session: AsyncSession, operator: Operator
) -> None:
    user = User(email="u@example.com")
    session.add(user)
    await session.flush()
    api_key = ApiKey(user_id=user.id, hash="h", prefix="pymth_live_x", label="t")
    session.add(api_key)
    await session.flush()

    await admin_service.create_sdk_approval(
        session,
        acting_operator=operator,
        lang="python",
        version="0.2.0",
        git_sha7="approved",
        status="approved",
        notes=None,
    )
    session.add(_make_session_row(user=user, api_key=api_key, sdk_identity="python/0.2.0/approved"))
    session.add(_make_session_row(user=user, api_key=api_key, sdk_identity="python/0.1.0/unknown"))
    session.add(_make_session_row(user=user, api_key=api_key, sdk_identity=None))
    session.add(_make_session_row(user=user, api_key=api_key, sdk_identity="garbage"))
    await session.flush()

    rows = await admin_service.list_recent_sessions_with_sdk(session, limit=10)
    by_ident = {ps.sdk_identity: status for (ps, status) in rows}
    assert by_ident["python/0.2.0/approved"] == "approved"
    assert by_ident["python/0.1.0/unknown"] == "unknown"
    assert by_ident[None] == "unknown"
    assert by_ident["garbage"] == "unknown"
