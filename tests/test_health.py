import pytest
from app.main import health


@pytest.mark.asyncio
async def test_health_reports_ok_with_db(session):
    assert await health(db=session) == {"ok": True}
