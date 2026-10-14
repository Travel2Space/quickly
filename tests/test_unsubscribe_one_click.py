import pytest
from sqlalchemy import func, select

from app.models import QueueSlot
from app.routers.tracking import unsubscribe_confirm_page, unsubscribe_one_click
from tests.conftest import (
    make_campaign, make_campaign_lead, make_inbox, make_lead, make_queue_slot, make_unsubscribe_token,
)


async def _setup(session):
    inbox = await make_inbox(session)
    a, b = await make_campaign(session, name="A"), await make_campaign(session, name="B")
    lead = await make_lead(session, email="eng@example.gr")
    cl_a, cl_b = await make_campaign_lead(session, a.id, lead.id), await make_campaign_lead(session, b.id, lead.id)
    for cl in (cl_a, cl_b):
        await make_queue_slot(session, cl.id, inbox.id)
    tok = await make_unsubscribe_token(session, lead_id=lead.id, campaign_id=a.id)
    return lead, tok


async def _all_slots(session):
    return (await session.execute(select(func.count(QueueSlot.id)))).scalar()


@pytest.mark.asyncio
async def test_get_shows_a_form_and_changes_nothing(session):
    lead, tok = await _setup(session)
    res = await unsubscribe_confirm_page(tok.token, db=session)
    assert res.status_code == 200
    assert f'action="/u/{tok.token}"' in res.body.decode() and 'method="post"' in res.body.decode()
    await session.refresh(lead)
    assert lead.suppressed_at is None and await _all_slots(session) == 2


@pytest.mark.asyncio
async def test_post_unsubscribes_globally(session):
    lead, tok = await _setup(session)
    res = await unsubscribe_one_click(tok.token, db=session)
    assert res.status_code == 200
    await session.refresh(lead)
    assert lead.suppressed_reason == "unsubscribe" and await _all_slots(session) == 0


@pytest.mark.asyncio
async def test_unknown_token_is_404_for_both(session):
    assert (await unsubscribe_confirm_page("nope", db=session)).status_code == 404
    assert (await unsubscribe_one_click("nope", db=session)).status_code == 404
