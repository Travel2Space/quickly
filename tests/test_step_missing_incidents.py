import logging

import pytest
from fastapi import BackgroundTasks
from sqlalchemy import delete

import app.jobs as jobs_mod
from app.models import Inbox
from app.routers.campaigns import create_sequence
from app.schemas import SequenceCreate
from app.send_prep import notify_once, reset_campaign_incidents
from tests.conftest import make_sequence
from tests.send_harness import enroll_due, make_open_campaign, make_smtp_inbox, wire


def test_reset_campaign_incidents_only_touches_that_campaign():
    assert notify_once(("step_missing", 1, 3))
    assert notify_once(("step_missing", 2, 3))
    assert notify_once(("blocked", 1, ("a",)))
    reset_campaign_incidents(1)
    assert notify_once(("step_missing", 1, 3))          # reopened
    assert not notify_once(("step_missing", 2, 3))      # untouched
    assert not notify_once(("blocked", 1, ("a",)))      # other kind untouched


@pytest.mark.asyncio
async def test_incident_reopens_after_the_steps_change(session, monkeypatch):
    inbox = await make_smtp_inbox(session)
    campaign = await make_open_campaign(session, inbox)
    await make_sequence(session, campaign.id, position=0)
    _, _, slot = await enroll_due(session, campaign, inbox, sequence_index=3)
    _, events = wire(monkeypatch, session)
    await jobs_mod.send_slot_job(slot.id)
    await jobs_mod.send_slot_job(slot.id)
    assert [e for e, _ in events] == ["send.step_missing"]
    # still missing (3 > 1) but the operator touched the steps: a fresh incident
    await create_sequence(campaign.id, SequenceCreate(position=1, subject="S", body="B"),
                          BackgroundTasks(), db=session)
    await jobs_mod.send_slot_job(slot.id)
    assert [e for e, _ in events] == ["send.step_missing", "send.step_missing"]


@pytest.mark.asyncio
async def test_repeat_is_not_logged_as_error(session, monkeypatch, caplog):
    inbox = await make_smtp_inbox(session)
    campaign = await make_open_campaign(session, inbox)
    await make_sequence(session, campaign.id, position=0)
    _, _, slot = await enroll_due(session, campaign, inbox, sequence_index=3)
    wire(monkeypatch, session)
    with caplog.at_level(logging.ERROR, logger="app.jobs"):
        await jobs_mod.send_slot_job(slot.id)
    assert "no step for index 3" in caplog.text
    caplog.clear()
    with caplog.at_level(logging.ERROR, logger="app.jobs"):
        await jobs_mod.send_slot_job(slot.id)
    assert caplog.text == ""


@pytest.mark.asyncio
async def test_missing_inbox_is_reported_as_such(session, monkeypatch, caplog):
    inbox = await make_smtp_inbox(session)
    campaign = await make_open_campaign(session, inbox)
    await make_sequence(session, campaign.id, position=0)
    _, _, slot = await enroll_due(session, campaign, inbox, sequence_index=0)
    await session.execute(delete(Inbox).where(Inbox.id == inbox.id))
    await session.flush()
    sent, events = wire(monkeypatch, session)
    with caplog.at_level(logging.ERROR, logger="app.jobs"):
        await jobs_mod.send_slot_job(slot.id)
    assert sent == [] and events == []
    assert "inbox is missing" in caplog.text and "no step" not in caplog.text
