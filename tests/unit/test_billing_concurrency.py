"""Regression coverage for serialized customer-balance mutations."""

from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from livepeer_open_clearinghouse.domains.billing import service as billing_service
from livepeer_open_clearinghouse.domains.billing.repo import CreditBalance, CreditLedger
from livepeer_open_clearinghouse.providers.db import Base


@pytest.mark.asyncio
async def test_locked_delta_refreshes_balance_loaded_before_competing_commit(
    tmp_path: Path,
) -> None:
    """A preflight-loaded ORM row must not overwrite a newer committed value."""

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'billing.db'}")
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    user_id = uuid.uuid4()
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        async with maker() as seed:
            seed.add(CreditBalance(user_id=user_id, amount_wei=Decimal(100), version=0))
            seed.add(CreditLedger(user_id=user_id, delta_wei=Decimal(100), reason="seed"))
            await seed.commit()

        async with maker() as stale, maker() as competing:
            preflight = await billing_service.get_balance(stale, user_id=user_id)
            assert preflight.amount_wei == Decimal(100)

            await billing_service._apply_delta(
                competing,
                user_id=user_id,
                delta_wei=Decimal(-10),
                reason="competing_debit",
            )
            await competing.commit()

            await billing_service._apply_delta(
                stale,
                user_id=user_id,
                delta_wei=Decimal(-10),
                reason="stale_session_debit",
            )
            await stale.commit()

        async with maker() as verify:
            balance = await verify.get(CreditBalance, user_id)
            ledger_total = await verify.scalar(
                select(func.sum(CreditLedger.delta_wei)).where(CreditLedger.user_id == user_id)
            )
            assert balance is not None
            assert balance.amount_wei == Decimal(80)
            assert balance.version == 2
            assert ledger_total == Decimal(80)
    finally:
        await engine.dispose()
