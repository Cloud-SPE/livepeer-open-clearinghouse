"""Integration-style tests for operator CRUD + role enforcement.

These run against an in-memory SQLite DB (aiosqlite). The service layer
doesn't use any Postgres-specific features here (no JSONB, no
`FOR UPDATE` locking — operator flow is small), so SQLite is faithful
enough. Anything that needs to verify Postgres-specific behaviour
should go in a real integration suite.

The conftest's ``test_settings`` fixture isn't used — we make our own
session here to keep these tests independent of the gateway lifespan.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# Pull every domain's repo so Base.metadata.create_all knows about it.
from livepeer_open_clearinghouse.domains.accounts import repo as _accounts  # noqa: F401
from livepeer_open_clearinghouse.domains.admin import service as admin_service
from livepeer_open_clearinghouse.domains.admin.repo import Operator
from livepeer_open_clearinghouse.domains.api_keys import repo as _api_keys  # noqa: F401
from livepeer_open_clearinghouse.domains.billing import repo as _billing  # noqa: F401
from livepeer_open_clearinghouse.domains.notifications import repo as _notif  # noqa: F401
from livepeer_open_clearinghouse.domains.payments import repo as _payments  # noqa: F401
from livepeer_open_clearinghouse.domains.usage import repo as _usage  # noqa: F401
from livepeer_open_clearinghouse.providers.clock import FrozenClock
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
async def owner_operator(session: AsyncSession) -> Operator:
    op = Operator(
        email="owner@example.com",
        name="Owner",
        token_hash="hash-not-checked-here",
        role="owner",
    )
    session.add(op)
    await session.flush()
    return op


@pytest.mark.unit
def test_generate_operator_token_shape() -> None:
    raw, h = admin_service._generate_operator_token()
    assert raw.startswith("loc_op_")
    # secrets.token_urlsafe(32) yields a ~43-char base64 string.
    assert len(raw) > len("loc_op_") + 30
    assert h != raw
    assert len(h) == 64  # sha256 hex


@pytest.mark.unit
async def test_create_operator_returns_raw_token_once(
    session: AsyncSession, owner_operator: Operator
) -> None:
    clock = FrozenClock(datetime(2026, 5, 23, tzinfo=UTC))
    op, raw = await admin_service.create_operator(
        session,
        acting_operator=owner_operator,
        email="newop@example.com",
        name="New Op",
        role="member",
        clock=clock,
    )
    assert op.email == "newop@example.com"
    assert op.role == "member"
    assert raw.startswith("loc_op_")
    # The raw token should NOT equal the stored hash.
    assert op.token_hash != raw


@pytest.mark.unit
async def test_create_operator_rejects_unknown_role(
    session: AsyncSession, owner_operator: Operator
) -> None:
    clock = FrozenClock(datetime(2026, 5, 23, tzinfo=UTC))
    with pytest.raises(admin_service.InvalidOperatorRole):
        await admin_service.create_operator(
            session,
            acting_operator=owner_operator,
            email="x@example.com",
            name="X",
            role="superuser",
            clock=clock,
        )


@pytest.mark.unit
async def test_create_operator_rejects_duplicate_email(
    session: AsyncSession, owner_operator: Operator
) -> None:
    clock = FrozenClock(datetime(2026, 5, 23, tzinfo=UTC))
    await admin_service.create_operator(
        session,
        acting_operator=owner_operator,
        email="dup@example.com",
        name="First",
        clock=clock,
    )
    with pytest.raises(admin_service.OperatorEmailTaken):
        await admin_service.create_operator(
            session,
            acting_operator=owner_operator,
            email="dup@example.com",
            name="Second",
            clock=clock,
        )


@pytest.mark.unit
async def test_update_operator_changes_name_and_role(
    session: AsyncSession, owner_operator: Operator
) -> None:
    clock = FrozenClock(datetime(2026, 5, 23, tzinfo=UTC))
    new_op, _ = await admin_service.create_operator(
        session,
        acting_operator=owner_operator,
        email="member@example.com",
        name="Member",
        clock=clock,
    )
    updated = await admin_service.update_operator(
        session,
        acting_operator=owner_operator,
        operator_id=new_op.id,
        name="Renamed",
        role="owner",
        clock=clock,
    )
    assert updated.name == "Renamed"
    assert updated.role == "owner"


@pytest.mark.unit
async def test_cannot_demote_last_owner(session: AsyncSession, owner_operator: Operator) -> None:
    """Demoting the only owner would brick the org."""
    clock = FrozenClock(datetime(2026, 5, 23, tzinfo=UTC))
    with pytest.raises(admin_service.CannotDemoteLastOwner):
        await admin_service.update_operator(
            session,
            acting_operator=owner_operator,
            operator_id=owner_operator.id,
            role="member",
            clock=clock,
        )


@pytest.mark.unit
async def test_revoke_self_forbidden(session: AsyncSession, owner_operator: Operator) -> None:
    clock = FrozenClock(datetime(2026, 5, 23, tzinfo=UTC))
    with pytest.raises(admin_service.CannotRevokeSelf):
        await admin_service.revoke_operator(
            session,
            acting_operator=owner_operator,
            operator_id=owner_operator.id,
            clock=clock,
        )


@pytest.mark.unit
async def test_cannot_revoke_last_owner(session: AsyncSession, owner_operator: Operator) -> None:
    """Same protection as demotion — except triggered by revoke, not role-change."""
    clock = FrozenClock(datetime(2026, 5, 23, tzinfo=UTC))
    # Create a second owner so we can have one operator try to revoke the original.
    second_owner, _ = await admin_service.create_operator(
        session,
        acting_operator=owner_operator,
        email="o2@example.com",
        name="Owner 2",
        role="owner",
        clock=clock,
    )
    # Now revoke the first one — fine, second owner still active.
    await admin_service.revoke_operator(
        session,
        acting_operator=second_owner,
        operator_id=owner_operator.id,
        clock=clock,
    )
    # And now revoking the last remaining owner blocks (acting as a member).
    member, _ = await admin_service.create_operator(
        session,
        acting_operator=second_owner,
        email="m@example.com",
        name="Member",
        clock=clock,
    )
    with pytest.raises(admin_service.CannotDemoteLastOwner):
        await admin_service.revoke_operator(
            session,
            acting_operator=member,
            operator_id=second_owner.id,
            clock=clock,
        )


@pytest.mark.unit
async def test_rotate_operator_token_changes_hash(
    session: AsyncSession, owner_operator: Operator
) -> None:
    clock = FrozenClock(datetime(2026, 5, 23, tzinfo=UTC))
    op, _ = await admin_service.create_operator(
        session,
        acting_operator=owner_operator,
        email="r@example.com",
        name="R",
        clock=clock,
    )
    old_hash = op.token_hash
    _, new_raw = await admin_service.rotate_operator_token(
        session,
        acting_operator=owner_operator,
        operator_id=op.id,
        clock=clock,
    )
    assert op.token_hash != old_hash
    assert new_raw.startswith("loc_op_")
