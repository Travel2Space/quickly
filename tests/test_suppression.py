import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.campaign_lead_status import campaign_lead_may_receive_sends, campaign_lead_schedule_eligibility_clause
from app.models import Campaign, CampaignLead, Lead, QueueSlot
from app.routers.leads import SuppressRequest, suppress_lead, unsuppress_lead
from app.suppression import is_suppressed, suppress_email
from tests.conftest import make_campaign, make_campaign_lead, make_inbox, make_lead, make_queue_slot


async def _slots(session, cl):
    return (await session.execute(select(func.count(QueueSlot.id)).where(QueueSlot.campaign_lead_id == cl.id))).scalar()


@pytest.mark.asyncio
async def test_suppression_is_global_across_campaigns_and_lead_rows(session):
    inbox = await make_inbox(session)
    a, b = await make_campaign(session, name="A"), await make_campaign(session, name="B")
    lead = await make_lead(session, email="Eng@Example.gr")
    twin = await make_lead(session, email="eng@example.gr")  # same address, second row
    cl_a, cl_b = await make_campaign_lead(session, a.id, lead.id), await make_campaign_lead(session, b.id, twin.id)
    await make_queue_slot(session, cl_a.id, inbox.id)
    await make_queue_slot(session, cl_b.id, inbox.id)

    assert await suppress_email(session, " ENG@example.gr ", "unsubscribe") == 2
    assert await suppress_email(session, "eng@example.gr", "bounce") == 0  # idempotent, reason kept
    await session.refresh(twin)
    assert twin.suppressed_at is not None and twin.suppressed_reason == "unsubscribe"
    assert await _slots(session, cl_a) == 0 and await _slots(session, cl_b) == 0
    assert not campaign_lead_may_receive_sends(cl_b, twin)
    eligible = (await session.execute(
        select(CampaignLead.id).join(Lead, CampaignLead.lead_id == Lead.id)
        .join(Campaign, CampaignLead.campaign_id == Campaign.id).where(campaign_lead_schedule_eligibility_clause())
    )).all()
    assert eligible == []


@pytest.mark.asyncio
async def test_suppressing_an_unknown_address_remembers_it(session):
    await suppress_email(session, "new@x.gr", "manual")
    assert await is_suppressed(session, "NEW@x.gr")


@pytest.mark.asyncio
async def test_api_suppress_and_admin_unsuppress(session):
    out = await suppress_lead(SuppressRequest(email="z@x.gr", reason="manual"), db=session)
    assert out["suppressed"] is True
    await unsuppress_lead(SuppressRequest(email="z@x.gr"), db=session, user=object())
    assert not await is_suppressed(session, "z@x.gr")


from app.routers.campaigns import bulk_add_leads_to_campaign
from app.schemas import CampaignLeadAdd
import app.jobs as jobs_mod
from app.sender import SendFailure
from tests.conftest import make_sequence
from tests.send_harness import enroll_due, make_open_campaign, make_smtp_inbox, wire


@pytest.mark.asyncio
async def test_adding_a_suppressed_address_is_skipped(session):
    c = await make_campaign(session)
    await suppress_email(session, "s@x.gr", "unsubscribe")
    out = await bulk_add_leads_to_campaign(
        c.id, [CampaignLeadAdd(email="s@x.gr"), CampaignLeadAdd(email="ok@x.gr")], db=session,
    )
    assert {r["email"]: r["status"] for r in out["results"]} == {"s@x.gr": "suppressed", "ok@x.gr": "added"}
    assert out["suppressed"] == 1
    n = (await session.execute(select(func.count(CampaignLead.id)).where(CampaignLead.campaign_id == c.id))).scalar()
    assert n == 1


@pytest.mark.asyncio
async def test_hard_bounce_on_send_suppresses_globally(session, monkeypatch):
    inbox = await make_smtp_inbox(session)
    c = await make_open_campaign(session, inbox)
    await make_sequence(session, c.id)
    lead, _, slot = await enroll_due(session, c, inbox, email="gone@x.gr")
    wire(monkeypatch, session)
    monkeypatch.setattr("app.jobs.send_email", lambda **kw: SendFailure(error_type="bounce", message="550 no such user"))
    await jobs_mod.send_slot_job(slot.id)
    await session.refresh(lead)
    assert lead.suppressed_reason == "bounce"


def _ndr(addr, mid):
    from datetime import datetime
    return {"subject": "Delivery Status Notification (Failure)", "message_id": mid,
            "in_reply_to": None, "references": [], "from": "mailer-daemon@googlemail.com",
            "to": ["sender@veltiono.eu"], "body_html": "", "snippet": "",
            "body_plain": f"Final-Recipient: rfc822; {addr}\nAction: failed\nStatus: 5.1.1\n",
            "date": datetime.utcnow()}


@pytest.mark.asyncio
async def test_ndr_suppresses_globally_for_every_lead_row(session, monkeypatch):
    import app.unibox as unibox
    from app.models import SmtpAccount

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr("app.webhooks.fire_webhook_event", _noop)
    inbox = await make_inbox(session, email="sender@veltiono.eu", provider="smtp")
    session.add(SmtpAccount(inbox_id=inbox.id, smtp_host="h", imap_host="i"))
    c = await make_campaign(session)
    lead = await make_lead(session, email="gone@x.gr")
    twin = await make_lead(session, email="gone@x.gr")
    cl1 = await make_campaign_lead(session, c.id, lead.id)
    cl2 = await make_campaign_lead(session, c.id, twin.id)
    await session.flush()
    await unibox._process_smtp_inbound_message(
        session, inbox=inbox, parsed=_ndr("gone@x.gr", "<ndr1@google.com>"), message_pk="7:9",
        thread_key="<ndr1@google.com>")
    for row in (lead, twin, cl1, cl2):
        await session.refresh(row)
    assert lead.suppressed_reason == "bounce" and twin.suppressed_reason == "bounce"
    assert cl1.enrollment_status == "bounced" and cl2.enrollment_status == "bounced"
