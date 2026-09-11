"""Wire-format helpers shared by every domain's pydantic view models.

Wei amounts are integers that routinely exceed 2**53, and a ``Decimal``
that came back from Postgres ``NUMERIC`` may carry an exponent
(``Decimal("1.20E+14")``). JSON numbers cannot carry either safely:
JavaScript consumers silently lose precision above 2**53 wei (about
0.009 ETH). Every wei field on an outbound model uses :data:`WeiDecimal`,
which serializes the exact integer as a decimal string. JavaScript callers
parse it with ``BigInt``. Inbound fields accept either an integer or an
integer string.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Annotated

from pydantic import BeforeValidator, PlainSerializer


def wei_to_wire(value: Decimal | int) -> str:
    """Canonical integer string for a wei amount (no exponent, no sign noise)."""
    if isinstance(value, int):
        return str(value)
    return str(int(value))


def _to_wei_decimal(value: object) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("wei amount must be an integer, not a boolean")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        text = value.strip()
        try:
            parsed = Decimal(text)
        except InvalidOperation as exc:
            raise ValueError("wei amount must be an integer string") from exc
        if parsed != parsed.to_integral_value():
            raise ValueError("wei amount must be a whole number")
        return parsed
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError("wei amount must be a whole number")
        return Decimal(int(value))
    raise ValueError("wei amount must be an integer or integer string")


WeiDecimal = Annotated[
    Decimal | int,
    BeforeValidator(_to_wei_decimal),
    PlainSerializer(wei_to_wire, return_type=str, when_used="json"),
]
"""A wei amount: ``Decimal`` in Python, integer string on the wire, int or string inbound."""
