"""Boundary tests for the mandatory wholesale authorization contract."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from livepeer_open_clearinghouse.domains.jobs.types import CreateJobRequest
from livepeer_open_clearinghouse.domains.sessions.types import (
    CreateSessionRequest,
    RefillSessionRequest,
)


@pytest.mark.unit
def test_job_requires_digest_and_caller_key() -> None:
    with pytest.raises(ValidationError) as exc_info:
        CreateJobRequest(
            capability="test:job",
            offering="default",
            transport="unary",
            estimated_units=1,
        )
    missing = {tuple(error["loc"]) for error in exc_info.value.errors()}
    assert {("workload_request_digest",), ("caller_public_key",)} <= missing


@pytest.mark.unit
def test_session_requires_preparation_digest_and_caller_key() -> None:
    with pytest.raises(ValidationError) as exc_info:
        CreateSessionRequest(
            capability="test:session",
            offering="default",
            descriptor_schema="test-runtime/v1",
            estimated_runway_units=1,
            max_total_units=2,
        )
    missing = {tuple(error["loc"]) for error in exc_info.value.errors()}
    assert {
        ("gateway_session_id",),
        ("preparation_token",),
        ("workload_request_digest",),
        ("caller_public_key",),
    } <= missing


@pytest.mark.unit
def test_session_refill_is_only_a_cumulative_authorization_revision() -> None:
    request = RefillSessionRequest(
        max_total_units=20,
        workload_request_digest="44" * 32,
    )
    assert request.max_total_units == 20
    assert "rebind_from" not in type(request).model_fields
    assert "replaces_request_id" not in type(request).model_fields

    with pytest.raises(ValidationError):
        RefillSessionRequest()


@pytest.mark.unit
def test_valid_wholesale_session_boundary() -> None:
    request = CreateSessionRequest(
        capability="test:session",
        offering="default",
        descriptor_schema="test-runtime/v1",
        estimated_runway_units=1,
        max_total_units=2,
        gateway_session_id=uuid.uuid4(),
        preparation_token="prepared",
        workload_request_digest="44" * 32,
        caller_public_key="02" + "55" * 32,
    )
    assert request.max_total_units == 2
