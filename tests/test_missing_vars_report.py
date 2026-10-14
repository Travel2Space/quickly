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


async def _lead(session, campaign, email, data, name="Test"):
    lead = await make_lead(session, email=email)
    lead.custom_data = data
    cl = await make_campaign_lead(session, campaign.id, lead.id)
    await session.flush()
    return lead, cl


@pytest.mark.asyncio
async def test_unused_fallback_is_not_reported_for_a_standard_step(session):
    c = await make_campaign(session)
    await make_sequence(session, c.id, subject="Hi", body="Body", fallback_subject="{{zzz}}", fallback_body="{{zzz}}")
    await _lead(session, c, "a@x.gr", {})
    assert await missing_vars_report(session, c.id) == {"leads_missing": 0, "variables": {}}


@pytest.mark.asyncio
async def test_preview_counts_only_for_html_steps(session):
    c = await make_campaign(session)
    seq = await make_sequence(session, c.id, subject="Hi", body="Plain body")
    seq.preview_text = "{{p}}"
    seq.is_html = False
    await _lead(session, c, "a@x.gr", {})
    assert (await missing_vars_report(session, c.id))["variables"] == {}
    seq.is_html = True
    await session.flush()
    assert (await missing_vars_report(session, c.id))["variables"] == {"p": 1}


@pytest.mark.asyncio
async def test_personalized_step_uses_each_leads_override_or_the_fallback(session):
    from app.models import CustomEmailOverride

    c = await make_campaign(session)
    seq = await make_sequence(session, c.id, subject="S", body="B", sequence_type="personalized",
                              fallback_subject="Fallback", fallback_body="Fallback {{city}}")
    _, cl_with = await _lead(session, c, "with@x.gr", {"city": "A"})
    _, cl_without = await _lead(session, c, "without@x.gr", {})
    session.add(CustomEmailOverride(campaign_lead_id=cl_with.id, sequence_id=seq.id,
                                    subject="Hello", body="Hi {{area}}", is_html=False))
    await session.flush()
    # `with`: override needs `area` (missing); the fallback's `city` is unused for them.
    # `without`: fallback needs `city` (missing).
    assert await missing_vars_report(session, c.id) == {
        "leads_missing": 2, "variables": {"area": 1, "city": 1}}


@pytest.mark.asyncio
async def test_disabled_variant_is_ignored_and_enabled_one_counts(session):
    from app.models import SequenceVariant

    c = await make_campaign(session)
    seq = await make_sequence(session, c.id, subject="Hi", body="Body")
    session.add_all([
        SequenceVariant(sequence_id=seq.id, label="off", body="{{off}}", enabled=False),
        SequenceVariant(sequence_id=seq.id, label="on", body="{{on}}", enabled=True),
    ])
    await _lead(session, c, "a@x.gr", {})
    await session.flush()
    assert (await missing_vars_report(session, c.id))["variables"] == {"on": 1}
