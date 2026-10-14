"""Shared helpers for tests that drive send_slot_job end to end."""
from __future__ import annotations

from datetime import datetime, timedelta

import app.jobs as jobs_mod
from app.models import EmailLog, SmtpAccount
from app.sender import SendResult
from tests.conftest import (
    make_campaign, make_campaign_inbox, make_campaign_lead, make_inbox, make_lead, make_queue_slot,
)


class SessionCtx:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return None


async def make_smtp_inbox(session, email: str = "sender@veltiono.eu"):
    inbox = await make_inbox(session, email=email, provider="smtp", wait_minutes_between=0)
    session.add(SmtpAccount(inbox_id=inbox.id, smtp_host="smtp.example.com", smtp_port=587,
                            smtp_username="u", smtp_password="p", smtp_use_tls=True))
    await session.flush()
    return inbox


async def make_open_campaign(session, inbox, **kw):
    """A campaign that may send at any time of any day, wired to *inbox*."""
    campaign = await make_campaign(session, sending_days=[0, 1, 2, 3, 4, 5, 6],
                                   sending_hours_start="00:00", sending_hours_end="23:59", **kw)
    await make_campaign_inbox(session, campaign.id, inbox.id)
    return campaign


async def enroll_due(session, campaign, inbox, *, email="lead@example.com", custom_data=None,
                     sequence_index=0, lead=None):
    """Enroll a lead (new unless *lead* given) with one slot that is already due."""
    if lead is None:
        lead = await make_lead(session, email=email)
        if custom_data is not None:
            lead.custom_data = custom_data
    cl = await make_campaign_lead(session, campaign.id, lead.id)
    slot = await make_queue_slot(session, cl.id, inbox.id, sequence_index=sequence_index,
                                 scheduled_date=datetime.utcnow() - timedelta(minutes=1))
    await session.flush()
    return lead, cl, slot


async def add_sent_log(session, lead, campaign, inbox, *, index, subject, message_id, thread_id, minutes_ago):
    """A previously sent step, as send_slot_job would have recorded it."""
    log = EmailLog(lead_id=lead.id, campaign_id=campaign.id, inbox_id=inbox.id, sequence_index=index,
                   subject=subject, message_id=message_id, thread_id=thread_id,
                   sent_at=datetime.utcnow() - timedelta(minutes=minutes_ago))
    session.add(log)
    await session.flush()
    return log


def wire(monkeypatch, session):
    """Point send_slot_job at *session*; capture send_email kwargs and fired events."""
    sent: list[dict] = []
    events: list[tuple[str, dict]] = []

    def fake_send(**kwargs):
        sent.append(kwargs)
        mid = f"<m{len(sent)}@veltiono.eu>"
        return SendResult(message_id=mid, thread_id=kwargs.get("thread_id") or mid)

    async def fake_event(db, event, data):
        events.append((event, data))

    monkeypatch.setattr("app.jobs.send_email", fake_send)
    monkeypatch.setattr("app.jobs.fire_webhook_event", fake_event)
    monkeypatch.setattr(jobs_mod, "AsyncSessionLocal", lambda: SessionCtx(session))
    return sent, events
