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
