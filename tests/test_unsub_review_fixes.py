import pytest

from app.app_settings import get_setting
from app.routers.settings import _PresendSettings, save_presend_settings


@pytest.mark.asyncio
async def test_presend_empty_or_absent_url_does_not_disable(session):
    await save_presend_settings(_PresendSettings(url="https://h.test/x"), db=session, user=object())
    await save_presend_settings(_PresendSettings(url=""), db=session, user=object())
    assert await get_setting(session, "presend_url") == "https://h.test/x"
    await save_presend_settings(_PresendSettings(timeout_seconds=3), db=session, user=object())
    assert await get_setting(session, "presend_url") == "https://h.test/x"


@pytest.mark.asyncio
async def test_presend_explicit_disable_stores_off_marker(session):
    await save_presend_settings(_PresendSettings(url="https://h.test/x"), db=session, user=object())
    await save_presend_settings(_PresendSettings(enabled=False), db=session, user=object())
    assert await get_setting(session, "presend_url") == ""


def test_notify_once_gap_suppressed_reopen_is_announced_later(monkeypatch):
    import app.send_prep as sp
    clock = [1000.0]
    monkeypatch.setattr(sp.time, "monotonic", lambda: clock[0])
    assert sp.notify_once("k", min_gap=60) is True
    sp.reset_notify("k")
    clock[0] += 10
    assert sp.notify_once("k", min_gap=60) is False  # silent: within gap
    clock[0] += 120
    assert sp.notify_once("k", min_gap=60) is True   # outage still on, gap over: announce
    assert sp.notify_once("k", min_gap=60) is False  # open now


def _cfg(**kw):
    import app.presend as p
    base = dict(url="https://h.test/x", secret="s", timeout=5.0, token_field="t", suppress_reasons=frozenset())
    base.update(kw)
    return p.PresendConfig(**base)


class _Lead:
    id = 1
    email = "a@x.gr"
    custom_data = {"t": "tok"}


@pytest.mark.asyncio
@pytest.mark.parametrize("kw", [
    {"url": "http://[bad"},          # InvalidURL / ValueError
    {"secret": "sécret-ключ"},       # UnicodeEncodeError building the header
    {"timeout": "abc"},              # bad timeout
])
async def test_presend_build_errors_become_unavailable(kw):
    import app.presend as p
    with pytest.raises(p.PresendUnavailable):
        await p.presend_check(_cfg(**kw), lead=_Lead(), campaign_id=1, step=0)


@pytest.mark.asyncio
async def test_presend_bad_timeout_setting_falls_back_to_5(session, monkeypatch):
    import app.presend as p
    monkeypatch.setenv("PRESEND_URL", "https://h.test/x")
    monkeypatch.setenv("PRESEND_TIMEOUT_SECONDS", "abc")
    assert (await p.load_presend_config(session)).timeout == 5.0
    monkeypatch.setenv("PRESEND_TIMEOUT_SECONDS", "-2")
    assert (await p.load_presend_config(session)).timeout == 5.0


from fastapi import BackgroundTasks, HTTPException

from app.models import Lead
from app.routers.leads import _lead_to_response, recover_lead
from app.schemas import LeadRecoverRequest
from app.suppression import is_suppressed, suppress_email
from tests.conftest import make_lead


@pytest.mark.asyncio
async def test_recovering_bounced_lead_clears_its_suppression(session):
    lead = await make_lead(session, "typo@x.gr")
    await suppress_email(session, "typo@x.gr", "bounce")
    await recover_lead(lead.id, LeadRecoverRequest(email="fixed@x.gr", verify_email=False),
                       BackgroundTasks(), db=session)
    await session.refresh(lead)
    assert lead.suppressed_at is None and lead.suppressed_reason is None
    assert not await is_suppressed(session, "fixed@x.gr")


@pytest.mark.asyncio
async def test_recovering_into_a_suppressed_address_is_refused(session):
    lead = await make_lead(session, "typo@x.gr")
    await suppress_email(session, "typo@x.gr", "bounce")
    await suppress_email(session, "Other@x.gr", "unsubscribe")
    with pytest.raises(HTTPException) as e:
        await recover_lead(lead.id, LeadRecoverRequest(email="other@x.gr", verify_email=False),
                           BackgroundTasks(), db=session)
    assert e.value.status_code == 422
    await session.refresh(lead)
    assert lead.suppressed_reason == "bounce"


@pytest.mark.asyncio
async def test_recover_keeps_non_bounce_suppression(session):
    lead = await make_lead(session, "u@x.gr")
    await suppress_email(session, "u@x.gr", "unsubscribe")
    await recover_lead(lead.id, LeadRecoverRequest(email="u2@x.gr", verify_email=False),
                       BackgroundTasks(), db=session)
    await session.refresh(lead)
    assert lead.suppressed_reason == "unsubscribe"


@pytest.mark.asyncio
async def test_lead_response_carries_suppression(session):
    lead = await make_lead(session, "s@x.gr")
    await suppress_email(session, "s@x.gr", "unsubscribe")
    from app.routers.leads import _lead_query_with_campaigns
    lead = (await session.execute(_lead_query_with_campaigns().where(Lead.id == lead.id))).scalar_one()
    out = _lead_to_response(lead, set(), set(), set())
    assert out.suppressed_at is not None and out.suppressed_reason == "unsubscribe"


import io

from sqlalchemy import func, select

from app.models import CampaignLead
from tests.conftest import make_campaign, make_campaign_inbox, make_inbox


async def _campaign(session):
    c = await make_campaign(session)
    ib = await make_inbox(session, email="own@test.com")
    await make_campaign_inbox(session, c.id, ib.id)
    await session.flush()
    return c


async def _lead_rows(session, email):
    return (await session.execute(select(func.count(Lead.id)).where(func.lower(Lead.email) == email))).scalar()


@pytest.mark.asyncio
async def test_bulk_add_mixed_case_suppressed_address(session):
    from app.routers.campaigns import bulk_add_leads_to_campaign
    from app.schemas import CampaignLeadAdd
    c = await _campaign(session)
    await make_lead(session, "Eng@X.gr")
    await suppress_email(session, "Eng@X.gr", "unsubscribe")
    out = await bulk_add_leads_to_campaign(c.id, [CampaignLeadAdd(email="eng@x.gr")], db=session)
    assert out["results"][0]["status"] == "suppressed"
    assert await _lead_rows(session, "eng@x.gr") == 1
    n = (await session.execute(select(func.count(CampaignLead.id)).where(CampaignLead.campaign_id == c.id))).scalar()
    assert n == 0


@pytest.mark.asyncio
async def test_csv_import_mixed_case_suppressed_address(session):
    from fastapi import UploadFile
    from app.routers.campaigns import import_campaign_leads
    c = await _campaign(session)
    await make_lead(session, "Eng@X.gr")
    await suppress_email(session, "Eng@X.gr", "unsubscribe")
    up = UploadFile(filename="l.csv", file=io.BytesIO(b"email,name\neng@x.gr,A\nok@x.gr,B\n"))
    out = await import_campaign_leads(c.id, up, db=session)
    assert out["suppressed"] == 1 and out["added"] == 1
    assert any(r["status"] == "suppressed" for r in out["results"]) if "results" in out else True
    assert await _lead_rows(session, "eng@x.gr") == 1


@pytest.mark.asyncio
async def test_unibox_fresh_compose_to_suppressed_refused_reply_allowed(session, monkeypatch):
    from app.models import GmailAccount, Inbox
    from app.routers import unibox as ub
    from app.routers.unibox import UniboxSendRequest
    from app.sender import SendResult
    inbox = Inbox(email="me@example.com", display_name="Me", provider="gmail")
    session.add(inbox)
    await session.flush()
    session.add(GmailAccount(inbox_id=inbox.id, google_email="me@example.com", access_token="t", refresh_token="r"))
    await session.flush()
    await suppress_email(session, "Gone@x.gr", "unsubscribe")
    monkeypatch.setattr("app.routers.unibox.send_email",
                        lambda **k: SendResult(message_id="<m@x>", thread_id="t1", gmail_message_id="g1"))
    with pytest.raises(HTTPException) as e:
        await ub.send_unibox_email(UniboxSendRequest(inbox_id=inbox.id, to_email="gone@x.gr", subject="s", body="b"), db=session)
    assert e.value.status_code == 422
    res = await ub.send_unibox_email(
        UniboxSendRequest(inbox_id=inbox.id, to_email="gone@x.gr", subject="s", body="b", in_reply_to="<x@y>"), db=session)
    assert res["ok"] is True


@pytest.mark.asyncio
async def test_inbox_test_mail_to_suppressed_refused(session):
    from app.routers import smtp as smtp_router
    await suppress_email(session, "gone@x.gr", "bounce")
    with pytest.raises(HTTPException) as e:
        await smtp_router.send_test_email(1, smtp_router.SendTestRequest(to_email="Gone@x.gr"), db=session)
    assert e.value.status_code == 422
