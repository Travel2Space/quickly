import pytest
from sqlalchemy import func, select

import app.jobs as jobs_mod
from app.models import LeadUnsubscribeToken
from app.sender import _build_email_message
from app.send_prep import missing_vars_report
from tests.conftest import make_sequence
from tests.send_harness import enroll_due, make_open_campaign, make_smtp_inbox, wire

TPL = "https://veltiono.gr/api/partners/unsubscribe?t={{veltiono_token}}"


async def _campaign(session, inbox):
    c = await make_open_campaign(session, inbox)
    c.list_unsubscribe_url_template = TPL
    c.list_unsubscribe_mailto = "partners@veltiono.eu"
    await session.flush()
    return c


@pytest.mark.asyncio
async def test_template_drives_headers_and_skips_builtin_token(session, monkeypatch):
    inbox = await make_smtp_inbox(session)
    c = await _campaign(session, inbox)
    await make_sequence(session, c.id, subject="Hi", body="Body")
    _, _, slot = await enroll_due(session, c, inbox, custom_data={"veltiono_token": "abc123def456"})
    sent, _ = wire(monkeypatch, session)
    await jobs_mod.send_slot_job(slot.id)
    assert sent[0]["list_unsubscribe_url"] == "https://veltiono.gr/api/partners/unsubscribe?t=abc123def456"
    assert sent[0]["list_unsubscribe_mailto"] == "partners@veltiono.eu"
    assert "/u/" not in sent[0]["body"]
    assert (await session.execute(select(func.count(LeadUnsubscribeToken.id)))).scalar() == 0


def test_header_lists_https_then_mailto():
    msg = _build_email_message(
        to_email="e@x.gr", subject="s", body="b", from_email="partners@veltiono.eu", from_name="",
        is_html=False, list_unsubscribe_url="https://veltiono.gr/api/partners/unsubscribe?t=abc123def456",
        list_unsubscribe_one_click=True, list_unsubscribe_mailto="partners@veltiono.eu",
    )
    assert msg["List-Unsubscribe"] == (
        "<https://veltiono.gr/api/partners/unsubscribe?t=abc123def456>, "
        "<mailto:partners@veltiono.eu?subject=unsubscribe>"
    )
    assert msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"


def test_header_is_https_only_without_mailto():
    msg = _build_email_message(
        to_email="e@x.gr", subject="s", body="b", from_email="partners@veltiono.eu", from_name="",
        is_html=False, list_unsubscribe_url="https://veltiono.gr/api/partners/unsubscribe?t=abc123def456",
    )
    assert msg["List-Unsubscribe"] == "<https://veltiono.gr/api/partners/unsubscribe?t=abc123def456>"


@pytest.mark.asyncio
async def test_missing_token_or_builtin_link_parks_the_slot(session, monkeypatch):
    inbox = await make_smtp_inbox(session)
    c = await _campaign(session, inbox)
    await make_sequence(session, c.id, subject="Hi", body="Stop: {{unsubscribe_link}}")
    _, _, slot = await enroll_due(session, c, inbox)
    sent, _ = wire(monkeypatch, session)
    await jobs_mod.send_slot_job(slot.id)
    await session.refresh(slot)
    assert sent == [] and slot.blocked_missing_vars == ["unsubscribe_link", "veltiono_token"]
    report = await missing_vars_report(session, c.id)
    assert report["variables"] == {"unsubscribe_link": 1, "veltiono_token": 1}


def test_schema_rejects_non_https_template():
    from pydantic import ValidationError

    from app.schemas import CampaignUpdate

    with pytest.raises(ValidationError):
        CampaignUpdate(list_unsubscribe_url_template="http://x.gr/u?t={{veltiono_token}}")
    assert CampaignUpdate(list_unsubscribe_url_template="  ").list_unsubscribe_url_template is None
