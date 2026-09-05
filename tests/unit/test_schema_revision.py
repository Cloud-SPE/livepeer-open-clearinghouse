from __future__ import annotations

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

    with pytest.raises(RuntimeError, match="expected 0023, found 0013"):
        await require_compatible_schema(session)
