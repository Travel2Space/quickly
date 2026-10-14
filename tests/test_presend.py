import json

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

import app.jobs as jobs_mod
import app.presend as presend
from app.models import QueueSlot
from tests.conftest import make_sequence
from tests.send_harness import enroll_due, make_open_campaign, make_smtp_inbox, wire

SECRET = "s" * 32


@pytest.fixture
def hook(monkeypatch):
    """Fake Veltiono: answers with whatever `hook.reply` holds; records requests."""
    state = {"reply": (200, {"allow": True}), "calls": []}

    def handler(request: httpx.Request):
        state["calls"].append(request)
        reply = state["reply"]
        if isinstance(reply, Exception):
            raise reply
        return httpx.Response(reply[0], json=reply[1])

    monkeypatch.setenv("PRESEND_URL", "https://veltiono.test/api/partners/outreach/may-send")
    monkeypatch.setenv("PRESEND_SECRET", SECRET)
    monkeypatch.setattr(presend, "_TRANSPORT", httpx.MockTransport(handler))
    return state


async def _due(session, custom=None):
    inbox = await make_smtp_inbox(session)
    c = await make_open_campaign(session, inbox)
    await make_sequence(session, c.id, subject="Hi", body="B")
    lead, _, slot = await enroll_due(session, c, inbox, email="eng@example.gr",
                                     custom_data={"veltiono_token": "abc123def456"} if custom is None else custom)
    return c, lead, slot


async def _slot_alive(session, slot):
    return (await session.execute(select(func.count(QueueSlot.id)).where(QueueSlot.id == slot.id))).scalar() == 1


@pytest.mark.asyncio
async def test_allow_sends_and_sends_the_contract(session, monkeypatch, hook):
    c, _, slot = await _due(session)
    sent, _ = wire(monkeypatch, session)
    await jobs_mod.send_slot_job(slot.id)
    assert len(sent) == 1
    req = hook["calls"][0]
    assert req.headers["authorization"] == f"Bearer {SECRET}"
    assert json.loads(req.content) == {"token": "abc123def456", "to_email": "eng@example.gr", "campaign_id": c.id, "step": 0}


@pytest.mark.asyncio
async def test_deny_blocks_and_suppresses(session, monkeypatch, hook):
    hook["reply"] = (200, {"allow": False, "reason": "do_not_contact"})
    _, lead, slot = await _due(session)
    sent, events = wire(monkeypatch, session)
    await jobs_mod.send_slot_job(slot.id)
    await session.refresh(lead)
    assert sent == [] and lead.suppressed_reason == "presend:do_not_contact"
    assert not await _slot_alive(session, slot)
    assert "presend.denied" in [e for e, _ in events]


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["email_mismatch", "unknown_source", "unknown_token"])
async def test_non_dnc_refusal_pauses_only_this_enrollment(session, monkeypatch, hook, reason):
    from app.models import CampaignLead

    hook["reply"] = (200, {"allow": False, "reason": reason})
    _, lead, slot = await _due(session)
    sent, events = wire(monkeypatch, session)
    await jobs_mod.send_slot_job(slot.id)
    await session.refresh(lead)
    cl = (await session.execute(select(CampaignLead).where(CampaignLead.lead_id == lead.id))).scalar_one()
    assert sent == [] and lead.suppressed_at is None and cl.sending_paused is True
    assert not await _slot_alive(session, slot)
    assert [d["reason"] for e, d in events if e == "presend.denied"] == [reason]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [(500, {"error": "failed"}), (200, {"nope": 1}), httpx.ReadTimeout("slow")])
async def test_outage_keeps_slot_notifies_once_then_recovers(session, monkeypatch, hook, failure):
    hook["reply"] = failure
    _, _, slot = await _due(session)
    sent, events = wire(monkeypatch, session)
    await jobs_mod.send_slot_job(slot.id)
    await jobs_mod.send_slot_job(slot.id)
    assert sent == [] and await _slot_alive(session, slot)
    assert [e for e, _ in events].count("presend.unavailable") == 1
    hook["reply"] = (200, {"allow": True})
    await jobs_mod.send_slot_job(slot.id)
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_lead_without_token_is_not_sent(session, monkeypatch, hook):
    _, _, slot = await _due(session, custom={})
    sent, _ = wire(monkeypatch, session)
    await jobs_mod.send_slot_job(slot.id)
    assert sent == [] and hook["calls"] == [] and await _slot_alive(session, slot)


@pytest.mark.asyncio
async def test_no_url_means_upstream_behaviour(session, monkeypatch, hook):
    monkeypatch.delenv("PRESEND_URL")
    _, _, slot = await _due(session)
    sent, _ = wire(monkeypatch, session)
    await jobs_mod.send_slot_job(slot.id)
    assert len(sent) == 1 and hook["calls"] == []


async def _send_test(session, monkeypatch, to_email):
    from app.routers.campaigns import TestEmailRequest, send_test_email
    from app.sender import SendResult

    c, _, _ = await _due(session)
    from app.models import Sequence
    seq = (await session.execute(select(Sequence).where(Sequence.campaign_id == c.id))).scalars().first()
    sent = []
    monkeypatch.setattr("app.sender.send_email",
                        lambda **kw: sent.append(kw) or SendResult(message_id="<t@x>", thread_id="<t@x>"))
    return sent, lambda: send_test_email(c.id, TestEmailRequest(sequence_id=seq.id, to_email=to_email), db=session)


@pytest.mark.asyncio
async def test_test_email_to_a_lead_is_checked(session, monkeypatch, hook):
    hook["reply"] = (200, {"allow": False, "reason": "do_not_contact"})
    sent, call = await _send_test(session, monkeypatch, "eng@example.gr")
    with pytest.raises(HTTPException) as exc:
        await call()
    assert exc.value.status_code == 422 and sent == []


@pytest.mark.asyncio
async def test_test_email_to_the_operator_skips_the_hook(session, monkeypatch, hook):
    sent, call = await _send_test(session, monkeypatch, "me@operator.test")
    out = await call()
    assert out["ok"] is True and hook["calls"] == [] and len(sent) == 1


@pytest.mark.asyncio
async def test_settings_roundtrip_never_returns_the_secret(session, monkeypatch):
    from app.routers.settings import _PresendSettings, get_presend_settings, save_presend_settings

    for k in ("PRESEND_URL", "PRESEND_SECRET"):
        monkeypatch.delenv(k, raising=False)
    assert (await get_presend_settings(db=session))["source"] == "off"
    await save_presend_settings(_PresendSettings(url="https://v.test/x", secret="k" * 32), db=session, user=object())
    out = await get_presend_settings(db=session)
    assert out["source"] == "settings" and out["secret_set"] is True and "secret" not in out
    await save_presend_settings(_PresendSettings(timeout_seconds=2), db=session, user=object())  # keeps secret
    assert (await get_presend_settings(db=session))["secret_set"] is True
    await save_presend_settings(_PresendSettings(reset=True), db=session, user=object())
    assert (await get_presend_settings(db=session))["source"] == "off"
