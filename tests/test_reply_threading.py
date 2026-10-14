import pytest
import app.jobs as jobs_mod
from app.send_prep import reply_subject
from tests.conftest import make_sequence
from tests.send_harness import add_sent_log, enroll_due, make_open_campaign, make_smtp_inbox, wire


@pytest.mark.parametrize("prev,expected", [("A", "Re: A"), ("re: A", "re: A"), ("RE:A", "RE:A"), ("  B ", "Re: B")])
def test_reply_subject(prev, expected):
    assert reply_subject(prev) == expected


@pytest.mark.asyncio
async def test_w4_replies_to_w3_not_w1(session, monkeypatch):
    inbox = await make_smtp_inbox(session)
    campaign = await make_open_campaign(session, inbox)
    for pos, subj in enumerate(["A", "", "B", ""]):
        await make_sequence(session, campaign.id, position=pos, subject=subj, body=f"W{pos + 1}")
    lead, _, slot = await enroll_due(session, campaign, inbox, sequence_index=3)
    for i, (subj, mid, tid, ago) in enumerate([("A", "<w1@x>", "t1", 30), ("Re: A", "<w2@x>", "t1", 20), ("B", "<w3@x>", "t3", 10)]):
        await add_sent_log(session, lead, campaign, inbox, index=i, subject=subj, message_id=mid, thread_id=tid, minutes_ago=ago)
    sent, _ = wire(monkeypatch, session)
    await jobs_mod.send_slot_job(slot.id)
    assert sent[0]["subject"] == "Re: B"
    assert sent[0]["reply_to_msg_id"] == "<w3@x>"
    # References = the thread W4 joins (W3 opened a new one), so the SMTP thread key
    # (`_send_via_smtp` takes references.split()[0]) is W3, not W1.
    assert sent[0]["references"] == "<w3@x>"


@pytest.mark.asyncio
async def test_w2_replies_to_w1(session, monkeypatch):
    inbox = await make_smtp_inbox(session)
    campaign = await make_open_campaign(session, inbox)
    await make_sequence(session, campaign.id, position=0, subject="A", body="W1")
    await make_sequence(session, campaign.id, position=1, subject="", body="W2")
    lead, _, slot = await enroll_due(session, campaign, inbox, sequence_index=1)
    await add_sent_log(session, lead, campaign, inbox, index=0, subject="A", message_id="<w1@x>", thread_id="t1", minutes_ago=10)
    sent, _ = wire(monkeypatch, session)
    await jobs_mod.send_slot_job(slot.id)
    assert (sent[0]["subject"], sent[0]["reply_to_msg_id"], sent[0]["references"]) == ("Re: A", "<w1@x>", "<w1@x>")


@pytest.mark.asyncio
async def test_chain_without_thread_ids_keeps_every_earlier_message(session, monkeypatch):
    inbox = await make_smtp_inbox(session)
    campaign = await make_open_campaign(session, inbox)
    for pos, subj in enumerate(["A", "B", ""]):
        await make_sequence(session, campaign.id, position=pos, subject=subj, body=f"W{pos + 1}")
    lead, _, slot = await enroll_due(session, campaign, inbox, sequence_index=2)
    await add_sent_log(session, lead, campaign, inbox, index=0, subject="A", message_id="<w1@x>", thread_id=None, minutes_ago=20)
    await add_sent_log(session, lead, campaign, inbox, index=1, subject="B", message_id="<w2@x>", thread_id=None, minutes_ago=10)
    sent, _ = wire(monkeypatch, session)
    await jobs_mod.send_slot_job(slot.id)
    assert sent[0]["subject"] == "Re: B"
    assert sent[0]["references"] == "<w1@x> <w2@x>"
