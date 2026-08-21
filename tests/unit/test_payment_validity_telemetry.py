"""Fail-closed handling of payer round and validity observations."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from livepeer_open_clearinghouse.domains.payments import service
from livepeer_open_clearinghouse.domains.payments.repo import PaymentDaemonDepositSnapshot
from livepeer_open_clearinghouse.providers.clock import FrozenClock


class _Session:
    def __init__(self, previous: PaymentDaemonDepositSnapshot | None = None) -> None:
        self.previous = previous
        self.added: PaymentDaemonDepositSnapshot | None = None

    async def scalar(self, _statement: object) -> PaymentDaemonDepositSnapshot | None:
        return self.previous

    def add(self, row: PaymentDaemonDepositSnapshot) -> None:
        self.added = row

    async def flush(self) -> None:
        return None


class _Daemon:
    def __init__(self, **overrides: object) -> None:
        values = {
            "deposit_wei": Decimal(100),
            "reserve_wei": Decimal(20),
            "withdraw_round": 0,
            "current_round": 4310,
            "ticket_validity_period": 2,
            "ticket_validity_period_observed_at": datetime(2026, 8, 21, 12, 0, tzinfo=UTC),
        }
        values.update(overrides)
        self.info = SimpleNamespace(**values)

    async def get_deposit_info(self) -> object:
        return self.info


@pytest.mark.unit
@pytest.mark.asyncio
async def test_snapshot_persists_fresh_validity_telemetry() -> None:
    session = _Session()
    row = await service.snapshot_deposit(
        session,  # type: ignore[arg-type]
        clock=FrozenClock(datetime(2026, 8, 21, 12, 1, tzinfo=UTC)),
        daemon=_Daemon(),
    )
    assert row.current_round == 4310
    assert row.ticket_validity_period == 2
    assert row.ticket_validity_period_observed_at == datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
    assert session.added is row


@pytest.mark.unit
@pytest.mark.asyncio
async def test_snapshot_rejects_regressing_round() -> None:
    previous = PaymentDaemonDepositSnapshot(
        taken_at=datetime(2026, 8, 21, 12, 0, tzinfo=UTC),
        deposit_wei=Decimal(100),
        reserve_wei=Decimal(20),
        withdraw_round=0,
        current_round=4310,
        ticket_validity_period=2,
        ticket_validity_period_observed_at=datetime(2026, 8, 21, 12, 0, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="current_round regressed"):
        await service.snapshot_deposit(
            _Session(previous),  # type: ignore[arg-type]
            clock=FrozenClock(),
            daemon=_Daemon(current_round=4309),
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_snapshot_accepts_equal_round_and_exposes_governance_drift() -> None:
    previous = PaymentDaemonDepositSnapshot(
        taken_at=datetime(2026, 8, 21, 12, 0, tzinfo=UTC),
        deposit_wei=Decimal(100),
        reserve_wei=Decimal(20),
        withdraw_round=0,
        current_round=4310,
        ticket_validity_period=2,
        ticket_validity_period_observed_at=datetime(2026, 8, 21, 12, 0, tzinfo=UTC),
    )
    row = await service.snapshot_deposit(
        _Session(previous),  # type: ignore[arg-type]
        clock=FrozenClock(),
        daemon=_Daemon(
            current_round=4310,
            ticket_validity_period=4,
            ticket_validity_period_observed_at=datetime(2026, 8, 21, 12, 1, tzinfo=UTC),
        ),
    )
    assert row.current_round == 4310
    assert row.ticket_validity_period == 4


@pytest.mark.unit
@pytest.mark.asyncio
async def test_snapshot_rejects_invalid_or_stale_observation() -> None:
    with pytest.raises(ValueError, match="invalid validity telemetry"):
        await service.snapshot_deposit(
            _Session(),  # type: ignore[arg-type]
            clock=FrozenClock(),
            daemon=_Daemon(ticket_validity_period=0),
        )

    previous = PaymentDaemonDepositSnapshot(
        taken_at=datetime(2026, 8, 21, 12, 0, tzinfo=UTC),
        deposit_wei=Decimal(100),
        reserve_wei=Decimal(20),
        withdraw_round=0,
        current_round=4310,
        ticket_validity_period=2,
        ticket_validity_period_observed_at=datetime(2026, 8, 21, 12, 0, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="observation time regressed"):
        await service.snapshot_deposit(
            _Session(previous),  # type: ignore[arg-type]
            clock=FrozenClock(),
            daemon=_Daemon(
                ticket_validity_period_observed_at=datetime(2026, 8, 21, 11, 59, tzinfo=UTC)
            ),
        )
