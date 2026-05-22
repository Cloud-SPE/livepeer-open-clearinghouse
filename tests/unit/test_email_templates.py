"""Unit tests for email templates."""

from __future__ import annotations

import pytest

from pymthouse.providers.email import templates


@pytest.mark.unit
def test_verification_email_carries_subject_link_text_html() -> None:
    link = "https://pymthouse.local/portal/#/verify-email?token=abc.def"
    msg = templates.verification_email(to="alice@example.com", verify_link=link)
    assert msg.to == "alice@example.com"
    assert "Verify your PymtHouse account" in msg.subject
    assert link in msg.text
    assert link in msg.html
    assert "expires in 24 hours" in msg.text


@pytest.mark.unit
def test_verification_email_escapes_html_in_link() -> None:
    link = 'https://pymt/?t="ev<il>"&x=1'
    msg = templates.verification_email(to="x@example.com", verify_link=link)
    # The raw quoted-with-angle-brackets payload appears only in the text body;
    # the HTML body has it escaped (so it can't escape from an attribute).
    assert link in msg.text
    assert "<il" not in msg.html
    assert "&quot;" in msg.html or "&#x27;" in msg.html or "&amp;" in msg.html


@pytest.mark.unit
def test_approval_notification_carries_portal_link() -> None:
    msg = templates.approval_notification_email(
        to="bob@example.com", public_base_url="https://pymt.example/"
    )
    assert msg.to == "bob@example.com"
    assert "approved" in msg.subject.lower()
    expected = "https://pymt.example/portal/"
    assert expected in msg.text
    assert expected in msg.html


@pytest.mark.unit
def test_approval_notification_strips_trailing_slash() -> None:
    msg = templates.approval_notification_email(
        to="c@example.com", public_base_url="https://pymt.example///"
    )
    # Single slash separation, no doubles.
    assert "https://pymt.example/portal/" in msg.text
    assert "https://pymt.example///portal" not in msg.text
