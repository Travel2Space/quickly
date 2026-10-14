"""The batch sender (run_send_job) must share the Q-5 / Q-8 behaviour of send_slot_job."""
from datetime import timedelta

import pytest

import app.jobs as jobs_mod
from app import time as time_provider
from tests.conftest import make_sequence
from tests.send_harness import add_sent_log, enroll_due, make_open_campaign, make_smtp_inbox, wire


async def _due_now(session, slot):
    slot.scheduled_date = time_provider.now() - timedelta(minutes=1)
    await session.flush()


@pytest.mark.asyncio
async def test_batch_sender_parks_a_slot_with_missing_variables(session, monkeypatch):
    inbox = await make_smtp_inbox(session)
    campaign = await make_open_campaign(session, inbox)
    await make_sequence(session, campaign.id, subject="Renovation in {{area}}", body="Hi {{name}}")
    _, _, slot = await enroll_due(session, campaign, inbox)
    await _due_now(session, slot)
    sent, events = wire(monkeypatch, session)
    await jobs_mod.run_send_job()
    assert sent == []
    await session.refresh(slot)
    assert slot.blocked_missing_vars == ["area"]
    assert [e for e, _ in events if e == "send.blocked_missing_vars"]


@pytest.mark.asyncio
async def test_batch_sender_skips_already_parked_slots(session, monkeypatch):
    inbox = await make_smtp_inbox(session)
    campaign = await make_open_campaign(session, inbox)
    await make_sequence(session, campaign.id, subject="Hi", body="B")
    _, _, slot = await enroll_due(session, campaign, inbox)
    await _due_now(session, slot)
    slot.blocked_missing_vars = ["area"]
    await session.flush()
    sent, _ = wire(monkeypatch, session)
    await jobs_mod.run_send_job()
    assert sent == []


@pytest.mark.asyncio
async def test_batch_sender_replies_to_the_latest_subject_in_its_own_thread(session, monkeypatch):
    inbox = await make_smtp_inbox(session)
    campaign = await make_open_campaign(session, inbox)
    for pos, subj in enumerate(["A", "", "B", ""]):
        await make_sequence(session, campaign.id, position=pos, subject=subj, body=f"W{pos + 1}")
    lead, _, slot = await enroll_due(session, campaign, inbox, sequence_index=3)
    await _due_now(session, slot)
    for i, (subj, mid, tid, ago) in enumerate(
        [("A", "<w1@x>", "t1", 30), ("Re: A", "<w2@x>", "t1", 20), ("B", "<w3@x>", "t3", 10)]
    ):
        await add_sent_log(session, lead, campaign, inbox, index=i, subject=subj,
                           message_id=mid, thread_id=tid, minutes_ago=ago)
    sent, _ = wire(monkeypatch, session)
    await jobs_mod.run_send_job()
    assert len(sent) == 1
    assert sent[0]["subject"] == "Re: B"
    assert sent[0]["reply_to_msg_id"] == "<w3@x>"
    assert sent[0]["references"] == "<w3@x>"
