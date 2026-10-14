import pytest
from app.routers.campaigns import get_missing_vars
from app.send_prep import missing_vars_report, template_var_names
from tests.conftest import make_campaign, make_campaign_lead, make_lead, make_sequence


def test_template_var_names():
    assert template_var_names("{{a}} {{ b }}", None, "{{a}}x") == {"a", "b"}


@pytest.mark.asyncio
async def test_report_counts_leads_and_variables(session):
    c = await make_campaign(session)
    await make_sequence(session, c.id, subject="{{area}} {{name}}", body="{{city}} {{unsubscribe_link}}")
    for email, data in [("a@x.gr", {"area": "A", "city": "C"}), ("b@x.gr", {"area": "A"}), ("c@x.gr", {})]:
        lead = await make_lead(session, email=email)
        lead.custom_data = data
        await make_campaign_lead(session, c.id, lead.id)
    await session.flush()
    expected = {"leads_missing": 2, "variables": {"area": 1, "city": 2}}
    assert await missing_vars_report(session, c.id) == expected
    assert await get_missing_vars(c.id, db=session) == expected
