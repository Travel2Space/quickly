import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import select

from app.models import Sequence
from app.routers.campaigns import create_sequence
from app.schemas import SequenceCreate
from tests.conftest import make_campaign, make_campaign_lead, make_lead


def _seq(position, subject="S"):
    return SequenceCreate(position=position, subject=subject, body="B", wait_days_after_previous=0)


@pytest.mark.asyncio
async def test_first_step_must_be_zero(session):
    c = await make_campaign(session)
    with pytest.raises(HTTPException) as e:
        await create_sequence(c.id, _seq(1), BackgroundTasks(), db=session)
    assert e.value.status_code == 422 and "0-based" in e.value.detail


@pytest.mark.asyncio
async def test_insert_in_the_middle_shifts_following_steps(session):
    c = await make_campaign(session)
    for p, s in [(0, "A"), (1, "C")]:
        await create_sequence(c.id, _seq(p, s), BackgroundTasks(), db=session)
    await create_sequence(c.id, _seq(1, "B"), BackgroundTasks(), db=session)
    rows = (await session.execute(
        select(Sequence.position, Sequence.subject).where(Sequence.campaign_id == c.id).order_by(Sequence.position)
    )).all()
    assert [tuple(r) for r in rows] == [(0, "A"), (1, "B"), (2, "C")]


@pytest.mark.asyncio
async def test_insert_in_the_middle_refused_once_leads_are_enrolled(session):
    c = await make_campaign(session)
    for p in (0, 1):
        await create_sequence(c.id, _seq(p), BackgroundTasks(), db=session)
    lead = await make_lead(session)
    await make_campaign_lead(session, c.id, lead.id)
    with pytest.raises(HTTPException) as e:
        await create_sequence(c.id, _seq(1), BackgroundTasks(), db=session)
    assert e.value.status_code == 422 and "enrolled" in e.value.detail
    await create_sequence(c.id, _seq(2), BackgroundTasks(), db=session)  # appending is fine


@pytest.mark.asyncio
async def test_gap_and_negative_rejected(session):
    c = await make_campaign(session)
    await create_sequence(c.id, _seq(0), BackgroundTasks(), db=session)
    for bad in (2, -1):
        with pytest.raises(HTTPException) as e:
            await create_sequence(c.id, _seq(bad), BackgroundTasks(), db=session)
        assert e.value.status_code == 422
