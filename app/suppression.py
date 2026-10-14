"""Global lead suppression (Q-3): an address that opted out, bounced or was refused by the
pre-send hook is never mailed again by any campaign."""
from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CampaignLead, Lead, QueueSlot
from app import time as time_provider


def _norm(email: str) -> str:
    return (email or "").strip().lower()


async def suppress_email(db: AsyncSession, email: str, reason: str) -> int:
    """Stamp every lead row with this address and drop their queued slots in all campaigns.

    Idempotent: an existing stamp (and its reason) is kept. An address with no lead row gets
    one, so a later import or API add sees the suppression. Returns rows newly stamped.
    """
    addr = _norm(email)
    if not addr:
        return 0
    leads = (await db.execute(select(Lead).where(func.lower(Lead.email) == addr))).scalars().all()
    if not leads:
        lead = Lead(email=addr, name="")
        db.add(lead)
        leads = [lead]
    now, newly = time_provider.utcnow(), 0
    for lead in leads:
        if lead.suppressed_at is None:
            lead.suppressed_at, lead.suppressed_reason = now, (reason or "manual")[:64]
            newly += 1
    await db.flush()
    lead_ids = [lead.id for lead in leads]
    await db.execute(delete(QueueSlot).where(QueueSlot.campaign_lead_id.in_(
        select(CampaignLead.id).where(CampaignLead.lead_id.in_(lead_ids)))))
    await db.flush()
    return newly


async def unsuppress_email(db: AsyncSession, email: str) -> int:
    leads = (await db.execute(select(Lead).where(func.lower(Lead.email) == _norm(email)))).scalars().all()
    for lead in leads:
        lead.suppressed_at, lead.suppressed_reason = None, None
    await db.flush()
    return len(leads)


async def is_suppressed(db: AsyncSession, email: str) -> bool:
    row = (await db.execute(select(Lead.id).where(
        func.lower(Lead.email) == _norm(email), Lead.suppressed_at.isnot(None)).limit(1))).first()
    return row is not None
