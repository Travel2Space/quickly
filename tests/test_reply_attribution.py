from datetime import datetime

import pytest
from sqlalchemy import func, select

import app.unibox as unibox
from app.models import EmailLog, LeadReply, QueueSlot, SmtpAccount
from tests.conftest import (
    make_campaign, make_campaign_inbox, make_campaign_lead, make_inbox, make_lead, make_queue_slot,
)


async def _noop(*a, **k):
    return None


@pytest.fixture
def events(monkeypatch):
    got = []

    async def fake_fwe(db, event, data):
        got.append((event, data))

    monkeypatch.setattr("app.webhooks.fire_webhook_event", fake_fwe)
    monkeypatch.setattr(unibox, "_fire_lead_reply_webhook_bg", _noop)
    monkeypatch.setattr(unibox, "_classify_and_notify_bg", _noop)
    monkeypatch.setattr(unibox.unibox_events, "publish", _noop)
    return got


async def _setup(session):
    inbox = await make_inbox(session, email="sender@veltiono.eu", provider="smtp")
    session.add(SmtpAccount(inbox_id=inbox.id, smtp_host="h", imap_host="i"))
    campaign = await make_campaign(session)
    await make_campaign_inbox(session, campaign.id, inbox.id)
    return inbox, campaign


async def _enrolled(session, campaign, inbox, email, w1_mid):
    lead = await make_lead(session, email=email)
    cl = await make_campaign_lead(session, campaign.id, lead.id)
    await make_queue_slot(session, cl.id, inbox.id, sequence_index=1)
    session.add(EmailLog(lead_id=lead.id, campaign_id=campaign.id, inbox_id=inbox.id, sequence_index=0,
                         subject="Hi", message_id=w1_mid, thread_id=w1_mid, sent_at=datetime.utcnow()))
    await session.flush()
    return lead, cl


def _parsed(frm, irt=None, refs=None, mid="<r1@gmail.com>"):
    return {"subject": "Re: Hi", "message_id": mid, "in_reply_to": irt, "references": refs or [],
            "from": frm, "to": ["sender@veltiono.eu"], "body_plain": "yes", "body_html": "",
            "snippet": "yes", "date": datetime.utcnow()}


async def _slots(session, cl):
    return (await session.execute(select(func.count(QueueSlot.id)).where(QueueSlot.campaign_lead_id == cl.id))).scalar()


@pytest.mark.asyncio
async def test_reply_from_other_address_stops_the_lead_we_wrote_to(session, events):
    inbox, campaign = await _setup(session)
    plus, plus_cl = await _enrolled(session, campaign, inbox, "a+x@example.com", "<w1a@veltiono.eu>")
    base, base_cl = await _enrolled(session, campaign, inbox, "a@example.com", "<w1b@veltiono.eu>")
    await unibox._process_smtp_inbound_message(
        session, inbox=inbox, parsed=_parsed("a@example.com", "<w1a@veltiono.eu>", ["<w1a@veltiono.eu>"]),
        message_pk="7:1", thread_key="<w1a@veltiono.eu>",
    )
    replied = {r for (r,) in (await session.execute(select(LeadReply.lead_id))).all()}
    assert replied == {plus.id}
    assert await _slots(session, plus_cl) == 0 and await _slots(session, base_cl) == 1


@pytest.mark.asyncio
async def test_newest_reference_wins(session, events):
    inbox, campaign = await _setup(session)
    first, _ = await _enrolled(session, campaign, inbox, "p@example.com", "<old@veltiono.eu>")
    second, _ = await _enrolled(session, campaign, inbox, "q@example.com", "<new@veltiono.eu>")
    await unibox._process_smtp_inbound_message(
        session, inbox=inbox, parsed=_parsed("z@example.com", None, ["<old@veltiono.eu>", "<new@veltiono.eu>"]),
        message_pk="7:2", thread_key="t",
    )
    assert {r for (r,) in (await session.execute(select(LeadReply.lead_id))).all()} == {second.id}


@pytest.mark.asyncio
async def test_ambiguous_sender_is_not_attached(session, events):
    inbox, campaign = await _setup(session)
    _, cl1 = await _enrolled(session, campaign, inbox, "dup@example.com", "<d1@veltiono.eu>")
    _, cl2 = await _enrolled(session, campaign, inbox, "dup@example.com", "<d2@veltiono.eu>")
    await unibox._process_smtp_inbound_message(
        session, inbox=inbox, parsed=_parsed("dup@example.com"), message_pk="7:3", thread_key="<r1@gmail.com>",
    )
    assert (await session.execute(select(func.count(LeadReply.id)))).scalar() == 0
    assert await _slots(session, cl1) == 1 and await _slots(session, cl2) == 1
    assert [e for e, _ in events] == ["reply.ambiguous_sender"]
