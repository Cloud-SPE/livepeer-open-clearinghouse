"""``from_response`` must map every gateway error body without raising."""

from __future__ import annotations

from livepeer_open_clearinghouse_sdk import InsufficientCredit, OpenClearinghouseError
from livepeer_open_clearinghouse_sdk.errors import from_response


def test_fastapi_validation_list_detail_maps_without_raising() -> None:
    detail = [
        {"type": "missing", "loc": ["body", "settlement", "signature"], "msg": "Field required"}
    ]
    err = from_response(status=422, body={"detail": detail}, retry_after=None)
    assert type(err) is OpenClearinghouseError
    assert err.status == 422
    assert err.code is None
    assert str(err).startswith('[{"type":"missing","loc":["body","settlement","signature"]')
    assert err.details == {"detail": detail}


def test_long_non_string_detail_is_truncated() -> None:
    detail = [{"msg": "x" * 2000}]
    err = from_response(status=422, body={"detail": detail}, retry_after=None)
    assert len(str(err)) == 500
    assert str(err).endswith("...")


def test_string_detail_still_acts_as_code_and_message() -> None:
    err = from_response(status=404, body={"detail": "no_route_available"}, retry_after=None)
    assert err.code == "no_route_available"
    assert str(err) == "no_route_available"


def test_error_envelope_takes_precedence() -> None:
    body = {
        "error": {
            "code": "INSUFFICIENT_CREDIT",
            "message": "not enough",
            "details": {"available_wei": "0"},
        }
    }
    err = from_response(status=402, body=body, retry_after=None)
    assert isinstance(err, InsufficientCredit)
    assert err.details == {"available_wei": "0"}


def test_non_dict_envelope_and_details_do_not_raise() -> None:
    err = from_response(status=500, body={"error": "boom", "detail": {"k": 1}}, retry_after=None)
    assert err.code is None
    assert str(err) == '{"k":1}'
    assert err.details == {"detail": {"k": 1}}
