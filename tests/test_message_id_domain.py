import pytest
import app.sender as sender_mod
from tests.test_smtp import _FakeSMTP, _smtp_account


@pytest.mark.parametrize("addr,dom", [("partners@Veltiono.EU", "veltiono.eu"), ("", None), ("nodomain", None), ("@x.gr", None)])
def test_msgid_domain(addr, dom):
    assert sender_mod._msgid_domain(addr) == dom


def test_smtp_message_id_carries_sender_domain(monkeypatch):
    monkeypatch.setattr("app.smtp_utils._smtp_connect", lambda account, timeout=30: _FakeSMTP("ok"))
    res = sender_mod._send_via_smtp(to_email="lead@example.com", subject="Hi", body="Hello",
                                    from_email="partners@veltiono.eu", smtp_account=_smtp_account())
    assert res.message_id.endswith("@veltiono.eu>")
