"""Wire-format helpers shared by every domain's pydantic view models.

Wei amounts are integers that routinely exceed 2**53, and a ``Decimal``
that came back from Postgres ``NUMERIC`` may carry an exponent
(``Decimal("1.20E+14")``). Pydantic's default JSON encoding of that value is
its ``str()`` form, so the wire showed ``"1.20E+14"``. Every wei field on
an outbound model uses :data:`WeiDecimal`, which serializes the exact
integer as a decimal string. JavaScript callers parse it with ``BigInt``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from pydantic import PlainSerializer


def wei_to_wire(value: Decimal | int) -> str:
    """Canonical integer string for a wei amount (no exponent, no sign noise)."""
    if isinstance(value, int):
        return str(value)
    return str(int(value))


WeiDecimal = Annotated[
    Decimal,
    PlainSerializer(wei_to_wire, return_type=str, when_used="json"),
]
"""A wei amount held as ``Decimal`` in Python and sent as an integer string."""
