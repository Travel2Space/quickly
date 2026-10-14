import pytest

from app.models import SmtpAccount
from app.routers import smtp as smtp_router
from tests.conftest import make_inbox


@pytest.mark.asyncio
async def test_put_without_intake_keys_keeps_stored_values(session):
    inbox = await make_inbox(session, email="s@veltiono.eu", provider="smtp")
    session.add(SmtpAccount(
        inbox_id=inbox.id, smtp_host="h", smtp_username="u", smtp_password="p", imap_host="i",
        imap_backfill_days=5, unibox_store_unmatched=False, unibox_ignore_senders="mailreach.co",
    ))
    await session.flush()
    data = smtp_router.SmtpAccountUpsert(smtp_host="h2", smtp_username="u", smtp_password="", imap_host="i", imap_username="u", imap_password="p")
    await smtp_router.upsert_smtp_account(inbox.id, data, db=session, _user=None)
    acct = (await session.execute(__import__("sqlalchemy").select(SmtpAccount))).scalar_one()
    assert acct.smtp_host == "h2"
    assert (acct.imap_backfill_days, acct.unibox_store_unmatched, acct.unibox_ignore_senders) == (5, False, "mailreach.co")
