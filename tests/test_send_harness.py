import pytest
import app.jobs as jobs_mod
from tests.conftest import make_sequence
from tests.send_harness import enroll_due, make_open_campaign, make_smtp_inbox, wire


@pytest.mark.asyncio
async def test_harness_sends_a_due_slot(session, monkeypatch):
    inbox = await make_smtp_inbox(session)
    campaign = await make_open_campaign(session, inbox)
    await make_sequence(session, campaign.id, subject="Hi {{name}}", body="Body")
    _, _, slot = await enroll_due(session, campaign, inbox)
    sent, _ = wire(monkeypatch, session)
    await jobs_mod.send_slot_job(slot.id)
    assert len(sent) == 1 and sent[0]["subject"] == "Hi Test Lead"
