import logging
import pytest
import app.jobs as jobs_mod
from tests.conftest import make_sequence
from tests.send_harness import enroll_due, make_open_campaign, make_smtp_inbox, wire


@pytest.mark.asyncio
async def test_slot_without_step_logs_error_and_notifies_once(session, monkeypatch, caplog):
    inbox = await make_smtp_inbox(session)
    campaign = await make_open_campaign(session, inbox)
    await make_sequence(session, campaign.id, position=0)
    _, _, slot = await enroll_due(session, campaign, inbox, sequence_index=3)
    sent, events = wire(monkeypatch, session)
    with caplog.at_level(logging.ERROR, logger="app.jobs"):
        await jobs_mod.send_slot_job(slot.id)
        await jobs_mod.send_slot_job(slot.id)
    assert sent == []
    assert "no step for index 3" in caplog.text
    assert [e for e, _ in events] == ["send.step_missing"]


@pytest.mark.asyncio
async def test_really_missing_slot_stays_quiet(session, monkeypatch):
    sent, events = wire(monkeypatch, session)
    await jobs_mod.send_slot_job(999_999)
    assert sent == [] and events == []
