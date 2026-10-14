"""Pre-send hook (Q-4): ask an external system before every email; never send without a yes."""
from __future__ import annotations

import os
from dataclasses import dataclass

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.app_settings import get_setting

_TRANSPORT: httpx.AsyncBaseTransport | None = None  # tests inject httpx.MockTransport


class PresendUnavailable(Exception):
    """No usable answer (network, timeout, non-200, bad body). The email is not sent."""


class PresendNoToken(PresendUnavailable):
    """The lead lacks the token field; nothing to ask about. The email is not sent."""


@dataclass(frozen=True)
class PresendConfig:
    url: str
    secret: str
    timeout: float
    token_field: str
    suppress_reasons: frozenset[str]


async def _value(db: AsyncSession, key: str, env: str, default: str) -> str:
    stored = await get_setting(db, key)  # Settings override env
    return (stored if stored is not None else os.getenv(env, default)).strip()


async def load_presend_config(db: AsyncSession) -> PresendConfig | None:
    url = await _value(db, "presend_url", "PRESEND_URL", "")
    if not url:
        return None
    return PresendConfig(
        url=url,
        secret=await _value(db, "presend_secret", "PRESEND_SECRET", ""),
        timeout=float(await _value(db, "presend_timeout_seconds", "PRESEND_TIMEOUT_SECONDS", "5") or 5),
        token_field=await _value(db, "presend_token_field", "PRESEND_TOKEN_FIELD", "veltiono_token") or "veltiono_token",
        suppress_reasons=frozenset(
            r.strip() for r in (await _value(
                db, "presend_suppress_reasons", "PRESEND_SUPPRESS_REASONS", "do_not_contact"
            )).split(",") if r.strip()
        ),
    )


async def presend_check(cfg: PresendConfig, *, lead, campaign_id: int, step: int) -> tuple[bool, str | None]:
    """(True, None) = send; (False, reason) = refused; PresendUnavailable = do not send now."""
    token = str((getattr(lead, "custom_data", None) or {}).get(cfg.token_field) or "").strip()
    if not token:
        raise PresendNoToken(f"lead {lead.id} has no {cfg.token_field}")
    payload = {"token": token, "to_email": lead.email, "campaign_id": campaign_id, "step": step}
    try:
        async with httpx.AsyncClient(timeout=cfg.timeout, transport=_TRANSPORT) as client:
            res = await client.post(cfg.url, json=payload, headers={"Authorization": f"Bearer {cfg.secret}"})
    except httpx.HTTPError as exc:
        raise PresendUnavailable(f"{type(exc).__name__}") from exc
    if res.status_code != 200:
        raise PresendUnavailable(f"HTTP {res.status_code}")
    try:
        body = res.json()
    except ValueError as exc:
        raise PresendUnavailable("response is not JSON") from exc
    if isinstance(body, dict) and body.get("allow") is True:
        return True, None
    if isinstance(body, dict) and body.get("allow") is False:
        return False, str(body.get("reason") or "denied")[:48]
    raise PresendUnavailable("unexpected response shape")
