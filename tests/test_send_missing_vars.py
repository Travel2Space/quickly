import asyncio
import pytest
from sqlalchemy import select

import app.jobs as jobs_mod
from app.models import QueueSlot
from app.send_prep import clear_missing_var_blocks
from tests.conftest import make_sequence
from tests.send_harness import enroll_due, make_open_campaign, make_smtp_inbox, wire


@pytest.mark.asyncio
async def test_missing_var_blocks_slot_until_lead_is_fixed(session, monkeypatch):
    inbox = await make_smtp_inbox(session)
    campaign = await make_open_campaign(session, inbox)
    await make_sequence(session, campaign.id, subject="Renovation in {{area}}", body="Hi {{name}}, {{offer}}")
    lead, _, slot = await enroll_due(session, campaign, inbox, custom_data={"offer": "x"})
    sent, events = wire(monkeypatch, session)

    await jobs_mod.send_slot_job(slot.id)
    assert sent == []
    await session.refresh(slot)
    assert slot.blocked_missing_vars == ["area"]
    ev = [d for e, d in events if e == "send.blocked_missing_vars"]
    assert ev and ev[0]["missing"] == ["area"] and ev[0]["lead_email"] == lead.email

    dispatched = []

    async def fake_dispatch(slot_id, delay):
        dispatched.append(slot_id)

    monkeypatch.setattr(jobs_mod, "_dispatch_slot", fake_dispatch)
    jobs_mod._pending_slot_ids.clear()
    await jobs_mod.run_slot_scan_job()
    await asyncio.sleep(0)
    assert slot.id not in dispatched  # parked slots are not re-fired every minute

    lead.custom_data = {"offer": "x", "area": "Thessaloniki"}
    await clear_missing_var_blocks(session, lead_id=lead.id)
    await session.flush()
    assert (await session.execute(select(QueueSlot.blocked_missing_vars).where(QueueSlot.id == slot.id))).scalar() is None
    await jobs_mod.send_slot_job(slot.id)
    assert len(sent) == 1 and sent[0]["subject"] == "Renovation in Thessaloniki"


@pytest.mark.asyncio
async def test_body_and_subject_vars_are_reported_together(session, monkeypatch):
    inbox = await make_smtp_inbox(session)
    campaign = await make_open_campaign(session, inbox)
    await make_sequence(session, campaign.id, subject="{{area}}", body="{{offer}}")
    _, _, slot = await enroll_due(session, campaign, inbox)
    sent, events = wire(monkeypatch, session)
    await jobs_mod.send_slot_job(slot.id)
    await session.refresh(slot)
    assert sent == [] and slot.blocked_missing_vars == ["area", "offer"]
