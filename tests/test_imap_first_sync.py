import pytest
from sqlalchemy import func, select

import app.unibox as unibox
from app.models import SmtpAccount, SmtpMessage, SmtpSyncState
from tests.conftest import make_inbox


def _raw(frm, mid, irt=None):
    h = (f"From: {frm}\r\nTo: sender@veltiono.eu\r\nSubject: Hi\r\nMessage-ID: {mid}\r\n"
         f"Date: Tue, 14 Oct 2026 10:00:00 +0000\r\n")
    if irt:
        h += f"In-Reply-To: {irt}\r\nReferences: {irt}\r\n"
    return (h + "\r\nhello\r\n").encode()


class FakeImap:
    def __init__(self, messages: dict[int, bytes], uidnext: int, since: list[int] | None = None, validity=7):
        self.messages, self.uidnext, self.since, self.validity = messages, uidnext, since or [], validity
        self.searches: list[str] = []

    def status(self, box, what):
        return "OK", [f"INBOX (UIDVALIDITY {self.validity} UIDNEXT {self.uidnext})".encode()]

    def uid(self, cmd, *args):
        if cmd == "search":
            crit = args[-1]
            self.searches.append(crit)
            if crit.startswith("SINCE"):
                return "OK", [" ".join(map(str, self.since)).encode()]
            lo = int(crit.split()[1].split(":")[0])
            return "OK", [" ".join(str(u) for u in sorted(self.messages) if u >= lo).encode()]
        return "OK", [(b"1 (RFC822)", self.messages[int(args[0])])]

    def logout(self):
        pass


async def _noop(*a, **k):
    return None


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr("app.webhooks.fire_webhook_event", _noop)
    monkeypatch.setattr(unibox, "_fire_lead_reply_webhook_bg", _noop)
    monkeypatch.setattr(unibox, "_classify_and_notify_bg", _noop)
    monkeypatch.setattr(unibox.unibox_events, "publish", _noop)


async def _inbox(session, **acct):
    inbox = await make_inbox(session, email="sender@veltiono.eu", provider="smtp")
    session.add(SmtpAccount(inbox_id=inbox.id, smtp_host="h", imap_host="i", **acct))
    await session.flush()
    return inbox


def _use(monkeypatch, fake):
    monkeypatch.setattr("app.smtp_utils._imap_connect", lambda account, timeout=15.0: fake)


async def _stored(session):
    return (await session.execute(select(func.count()).select_from(SmtpMessage))).scalar()


@pytest.mark.asyncio
async def test_first_sync_starts_from_now(session, monkeypatch):
    inbox = await _inbox(session)
    old = {u: _raw("someone@else.com", f"<o{u}@x>") for u in range(1, 2001)}
    _use(monkeypatch, FakeImap(old, uidnext=2001))
    await unibox._sync_inbox_smtp(session, inbox)
    state = (await session.execute(select(SmtpSyncState))).scalar_one()
    assert state.last_uid == 2000 and await _stored(session) == 0


@pytest.mark.asyncio
async def test_unmatched_and_ignored_senders(session, monkeypatch):
    inbox = await _inbox(session, unibox_store_unmatched=False, unibox_ignore_senders="mailreach.co\nwarm@pool.io")
    session.add(SmtpSyncState(inbox_id=inbox.id, uidvalidity=7, last_uid=10))
    await session.flush()
    msgs = {11: _raw("stranger@x.com", "<s@x>"), 12: _raw("bot@eu.mailreach.co", "<m@x>"),
            13: _raw("warm@pool.io", "<w@x>")}
    _use(monkeypatch, FakeImap(msgs, uidnext=14))
    await unibox._sync_inbox_smtp(session, inbox)
    state = (await session.execute(select(SmtpSyncState))).scalar_one()
    assert await _stored(session) == 0 and state.last_uid == 13


@pytest.mark.asyncio
async def test_ignored_sender_dropped_even_when_storing_unmatched(session, monkeypatch):
    inbox = await _inbox(session, unibox_ignore_senders="@mailreach.co")
    session.add(SmtpSyncState(inbox_id=inbox.id, uidvalidity=7, last_uid=10))
    await session.flush()
    _use(monkeypatch, FakeImap({11: _raw("a@mailreach.co", "<m@x>"), 12: _raw("human@x.com", "<h@x>")}, uidnext=13))
    await unibox._sync_inbox_smtp(session, inbox)
    assert await _stored(session) == 1


@pytest.mark.asyncio
async def test_backfill_days_pulls_recent_history(session, monkeypatch):
    inbox = await _inbox(session, imap_backfill_days=3)
    fake = FakeImap({u: _raw("h@x.com", f"<b{u}@x>") for u in (5, 6, 7)}, uidnext=8, since=[6, 7])
    _use(monkeypatch, fake)
    await unibox._sync_inbox_smtp(session, inbox)
    assert any(s.startswith("SINCE ") for s in fake.searches)
    assert await _stored(session) == 2
    assert (await session.execute(select(SmtpSyncState))).scalar_one().last_uid == 7


@pytest.mark.asyncio
async def test_lead_reply_kept_when_unmatched_mail_is_dropped(session, monkeypatch):
    from datetime import datetime
    from app.models import EmailLog
    from tests.conftest import make_campaign, make_lead

    inbox = await _inbox(session, unibox_store_unmatched=False)
    session.add(SmtpSyncState(inbox_id=inbox.id, uidvalidity=7, last_uid=10))
    campaign = await make_campaign(session)
    lead = await make_lead(session, email="eng@example.gr")
    from tests.conftest import make_campaign_lead, make_queue_slot
    cl = await make_campaign_lead(session, campaign.id, lead.id)
    await make_queue_slot(session, cl.id, inbox.id, sequence_index=1)
    session.add(EmailLog(lead_id=lead.id, campaign_id=campaign.id, inbox_id=inbox.id, sequence_index=0,
                         subject="Hi", message_id="<w1@veltiono.eu>", thread_id="<w1@veltiono.eu>",
                         sent_at=datetime.utcnow()))
    await session.flush()
    msgs = {11: _raw("stranger@x.com", "<s@x>"), 12: _raw("other@x.com", "<r@x>", irt="<w1@veltiono.eu>")}
    _use(monkeypatch, FakeImap(msgs, uidnext=13))
    await unibox._sync_inbox_smtp(session, inbox)
    assert await _stored(session) == 1  # the reply quoting our email, from any address
    from app.models import LeadReply, QueueSlot
    assert (await session.execute(select(func.count()).select_from(LeadReply).where(LeadReply.lead_id == lead.id))).scalar() == 1
    assert (await session.execute(select(func.count()).select_from(QueueSlot))).scalar() == 0


@pytest.mark.asyncio
async def test_ndr_survives_unstored_unmatched(session, monkeypatch):
    inbox = await _inbox(session, unibox_store_unmatched=False)
    session.add(SmtpSyncState(inbox_id=inbox.id, uidvalidity=7, last_uid=10))
    await session.flush()
    _use(monkeypatch, FakeImap({11: _raw("MAILER-DAEMON@mx.example.com", "<ndr@x>")}, uidnext=12))
    await unibox._sync_inbox_smtp(session, inbox)
    assert await _stored(session) == 1


@pytest.mark.asyncio
async def test_recreated_mailbox_rebaselines_instead_of_reimporting(session, monkeypatch):
    inbox = await _inbox(session)
    session.add(SmtpSyncState(inbox_id=inbox.id, uidvalidity=6, last_uid=50))
    await session.flush()
    _use(monkeypatch, FakeImap({u: _raw("h@x.com", f"<n{u}@x>") for u in range(1, 300)}, uidnext=300, validity=7))
    await unibox._sync_inbox_smtp(session, inbox)
    state = (await session.execute(select(SmtpSyncState))).scalar_one()
    assert (state.uidvalidity, state.last_uid) == (7, 299) and await _stored(session) == 0


class NoUidNextImap(FakeImap):
    def status(self, box, what):
        return "OK", [f"INBOX (UIDVALIDITY {self.validity})".encode()]


@pytest.mark.asyncio
async def test_unparseable_status_changes_nothing(session, monkeypatch):
    inbox = await _inbox(session)
    fake = NoUidNextImap({1: _raw("h@x.com", "<a@x>")}, uidnext=2)
    _use(monkeypatch, fake)
    await unibox._sync_inbox_smtp(session, inbox)
    state = (await session.execute(select(SmtpSyncState))).scalar_one()
    assert state.uidvalidity is None and (state.last_uid or 0) == 0
    assert fake.searches == [] and await _stored(session) == 0


@pytest.mark.asyncio
async def test_ignored_domain_still_stores_reply_quoting_our_email(session, monkeypatch):
    from datetime import datetime
    from app.models import EmailLog
    from tests.conftest import make_campaign, make_lead

    inbox = await _inbox(session, unibox_ignore_senders="example.gr")
    session.add(SmtpSyncState(inbox_id=inbox.id, uidvalidity=7, last_uid=10))
    campaign = await make_campaign(session)
    lead = await make_lead(session, email="eng@example.gr")
    session.add(EmailLog(lead_id=lead.id, campaign_id=campaign.id, inbox_id=inbox.id, sequence_index=0,
                         subject="Hi", message_id="<w1@veltiono.eu>", thread_id="<w1@veltiono.eu>",
                         sent_at=datetime.utcnow()))
    await session.flush()
    _use(monkeypatch, FakeImap({11: _raw("eng@example.gr", "<r@x>", irt="<w1@veltiono.eu>")}, uidnext=12))
    await unibox._sync_inbox_smtp(session, inbox)
    assert await _stored(session) == 1


@pytest.mark.parametrize("addr,rules,expected", [
    ("a@x.com", "a@x.com", True),
    ("A@X.com", "a@x.com", True),
    ("a@mailreach.co", "@mailreach.co", True),
    ("a@mailreach.co", "mailreach.co", True),
    ("a@eu.mailreach.co", "mailreach.co", True),
    ("a@notmailreach.co", "mailreach.co", False),
    ("b@x.com", "a@x.com", False),
    ("a@x.com", "", False),
])
def test_sender_ignored(addr, rules, expected):
    assert unibox._sender_ignored(addr, rules) is expected
