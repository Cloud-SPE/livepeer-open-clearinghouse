from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from livepeer_open_clearinghouse.providers.db import (
    EXPECTED_ALEMBIC_REVISION,
    require_compatible_schema,
)


@pytest.mark.unit
def test_expected_schema_revision_matches_alembic_head() -> None:
    scripts = ScriptDirectory.from_config(Config("alembic.ini"))

    assert scripts.get_current_head() == EXPECTED_ALEMBIC_REVISION


@pytest.mark.unit
async def test_schema_guard_accepts_exact_revision() -> None:
    result = MagicMock()
    result.scalar_one_or_none.return_value = EXPECTED_ALEMBIC_REVISION
    session = AsyncMock()
    session.execute.return_value = result

    await require_compatible_schema(session)


@pytest.mark.unit
async def test_schema_guard_rejects_old_or_new_revision() -> None:
    result = MagicMock()
    result.scalar_one_or_none.return_value = "0013"
    session = AsyncMock()
    session.execute.return_value = result

    with pytest.raises(RuntimeError, match="expected 0027, found 0013"):
        await require_compatible_schema(session)


@pytest.mark.unit
def test_wholesale_cutover_migration_blocks_active_legacy_engagements() -> None:
    migration = (
        Path(__file__).parents[2] / "migrations/versions/0027_wholesale_only_cutover.py"
    ).read_text()

    assert "accounting_mode = 'legacy_ticket'" in migration
    assert "state IN ('open', 'draining')" in migration
    assert "p.status IN ('reserved', 'issued')" in migration
    assert "p.session_id IS NULL OR s.state IN ('open', 'draining')" in migration
    assert 'server_default="wholesale_account"' in migration
    assert "cannot be downgraded" in migration
