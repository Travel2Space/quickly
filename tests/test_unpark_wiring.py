"""Un-parking of slots held for missing template variables, via the real routers."""
import pytest
from fastapi import BackgroundTasks
from sqlalchemy import select

import app.jobs as jobs_mod
from app.models import QueueSlot
from app.routers.campaigns import bulk_add_leads_to_campaign, update_campaign, write_custom_email
from app.routers.leads import update_lead
from app.schemas import CampaignLeadAdd, CampaignUpdate, CustomEmailWrite, LeadUpdate
from tests.conftest import make_campaign, make_sequence
from tests.send_harness import enroll_due, make_open_campaign, make_smtp_inbox, wire


async def _parked(session, slot):
    slot.blocked_missing_vars = ["area"]
    await session.flush()


async def _blocked(session, slot_id):
    return (await session.execute(
        select(QueueSlot.blocked_missing_vars).where(QueueSlot.id == slot_id)
    )).scalar()


@pytest.mark.asyncio
async def test_custom_email_write_unparks_the_lead(session):
    inbox = await make_smtp_inbox(session)
    campaign = await make_open_campaign(session, inbox)
    seq = await make_sequence(session, campaign.id, sequence_type="personalized")
    lead, _, slot = await enroll_due(session, campaign, inbox)
    await _parked(session, slot)
    await write_custom_email(campaign.id, lead.id, seq.id,
                             CustomEmailWrite(subject="S", body="B"), BackgroundTasks(), db=session)
    assert await _blocked(session, slot.id) is None


@pytest.mark.asyncio
async def test_unrelated_campaign_patch_does_not_unpark(session):
    inbox = await make_smtp_inbox(session)
    campaign = await make_open_campaign(session, inbox)
    await make_sequence(session, campaign.id)
    _, _, slot = await enroll_due(session, campaign, inbox)
    await _parked(session, slot)
    await update_campaign(campaign.id, CampaignUpdate(name="Renamed"), BackgroundTasks(), db=session)
    assert await _blocked(session, slot.id) == ["area"]


@pytest.mark.asyncio
async def test_parking_the_same_slot_twice_notifies_once(session, monkeypatch):
    inbox = await make_smtp_inbox(session)
    campaign = await make_open_campaign(session, inbox)
    await make_sequence(session, campaign.id, subject="{{area}}", body="B")
    _, _, slot = await enroll_due(session, campaign, inbox)
    sent, events = wire(monkeypatch, session)
    await jobs_mod.send_slot_job(slot.id)
    slot.blocked_missing_vars = None          # un-parked by an unrelated edit
    await session.flush()
    await jobs_mod.send_slot_job(slot.id)
    assert sent == []
    assert [e for e, _ in events].count("send.blocked_missing_vars") == 1


@pytest.mark.asyncio
async def test_parked_slot_is_not_processed(session, monkeypatch):
    inbox = await make_smtp_inbox(session)
    campaign = await make_open_campaign(session, inbox)
    await make_sequence(session, campaign.id, subject="Hi", body="B")   # nothing missing
    _, _, slot = await enroll_due(session, campaign, inbox)
    await _parked(session, slot)
    sent, events = wire(monkeypatch, session)
    await jobs_mod.send_slot_job(slot.id)
    assert sent == [] and events == []


@pytest.mark.asyncio
async def test_lead_patch_unparks(session):
    inbox = await make_smtp_inbox(session)
    campaign = await make_open_campaign(session, inbox)
    await make_sequence(session, campaign.id)
    lead, _, slot = await enroll_due(session, campaign, inbox)
    await _parked(session, slot)
    await update_lead(lead.id, LeadUpdate(custom_data={"area": "X"}), BackgroundTasks(), db=session)
    assert await _blocked(session, slot.id) is None


@pytest.mark.asyncio
async def test_bulk_add_of_existing_lead_with_new_data_unparks(session):
    inbox = await make_smtp_inbox(session)
    campaign = await make_open_campaign(session, inbox)
    await make_sequence(session, campaign.id)
    lead, _, slot = await enroll_due(session, campaign, inbox, email="e@x.gr")
    lead.custom_data = {}
    await _parked(session, slot)
    other = await make_campaign(session, name="Other")
    await bulk_add_leads_to_campaign(
        other.id, [CampaignLeadAdd(email="e@x.gr", custom_data={"area": "X"})], db=session)
    assert await _blocked(session, slot.id) is None
