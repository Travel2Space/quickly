"""Send-time helpers shared by the send paths in app/jobs.py and the campaign routers."""
from __future__ import annotations

import re
import time
from typing import Hashable

# Process-local de-duplication of operator notifications. The slot scanner re-fires
# a skipped slot every minute; an incident must notify once, not once a minute.
# A restart may repeat a notification once — acceptable.
_open: set[Hashable] = set()             # incidents currently open
_last_sent: dict[Hashable, float] = {}   # key -> monotonic time of the last notification


def notify_once(key: Hashable, min_gap: float = 0.0) -> bool:
    """True when an incident for *key* opens and should be announced.

    Silent while the incident is open (until reset_notify). *min_gap* seconds also
    silence a re-opening soon after the last announcement, so a flapping cause
    cannot notify every minute.
    """
    if key in _open:
        return False
    _open.add(key)
    now = time.monotonic()
    if now - _last_sent.get(key, float("-inf")) < min_gap:
        return False
    _last_sent[key] = now
    return True


def reset_notify(key: Hashable) -> None:
    """The incident is over; the next failure may announce again (subject to min_gap)."""
    _open.discard(key)


def reset_campaign_incidents(campaign_id: int) -> None:
    """The campaign's steps changed: step_missing incidents may announce again."""
    for key in [k for k in _open if isinstance(k, tuple) and k[:2] == ("step_missing", campaign_id)]:
        _open.discard(key)


def reset_for_tests() -> None:
    _open.clear()
    _last_sent.clear()


async def clear_missing_var_blocks(db, *, lead_id: int | None = None, campaign_id: int | None = None) -> None:
    """Un-park slots after the data or the template changed; the next scan retries them."""
    from sqlalchemy import select, update
    from app.models import CampaignLead, QueueSlot

    cl_ids = select(CampaignLead.id)
    if lead_id is not None:
        cl_ids = cl_ids.where(CampaignLead.lead_id == lead_id)
    if campaign_id is not None:
        cl_ids = cl_ids.where(CampaignLead.campaign_id == campaign_id)
    await db.execute(
        update(QueueSlot)
        .where(QueueSlot.campaign_lead_id.in_(cl_ids), QueueSlot.blocked_missing_vars.isnot(None))
        .values(blocked_missing_vars=None)
    )


def render_all_strict(texts: dict, lead_data: dict) -> dict:
    """Strict-render several templates; one MissingTemplateVars naming every gap across all."""
    from app.sender import MissingTemplateVars, render_body

    out, missing = {}, []
    for key, text in texts.items():
        if text is None:
            out[key] = None
            continue
        try:
            out[key] = render_body(text, lead_data, strict=True)
        except MissingTemplateVars as exc:
            missing.extend(exc.names)
    if missing:
        raise MissingTemplateVars(missing)
    return out


def template_var_names(*texts) -> set[str]:
    from app.sender import _TEMPLATE_VAR_RE

    names: set[str] = set()
    for text in texts:
        if text:
            names.update(m.strip() for m in _TEMPLATE_VAR_RE.findall(text))
    return names


async def missing_vars_report(db, campaign_id: int) -> dict:
    """Before anything is sent: how many enrolled leads lack which template variables."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    from app.models import CampaignLead, Lead, Sequence
    from app.sender import _BUILTIN_TEMPLATE_VARS, get_lead_data

    seqs = (await db.execute(
        select(Sequence).options(selectinload(Sequence.variants)).where(Sequence.campaign_id == campaign_id)
    )).scalars().all()
    texts: list = []
    for s in seqs:
        texts += [s.subject, s.body, s.preview_text, s.fallback_subject, s.fallback_body]
        texts += [t for v in s.variants if v.enabled for t in (v.subject, v.body, v.preview_text)]
    needed = template_var_names(*texts) - _BUILTIN_TEMPLATE_VARS - {"unsubscribe_link"}
    if not needed:
        return {"leads_missing": 0, "variables": {}}
    leads = (await db.execute(
        select(Lead).join(CampaignLead, CampaignLead.lead_id == Lead.id).where(CampaignLead.campaign_id == campaign_id)
    )).scalars().all()
    counts: dict[str, int] = {}
    leads_missing = 0
    for lead in leads:
        data = get_lead_data(lead)
        gaps = [n for n in needed if data.get(n) is None or not str(data.get(n)).strip()]
        if gaps:
            leads_missing += 1
            for n in gaps:
                counts[n] = counts.get(n, 0) + 1
    return {"leads_missing": leads_missing, "variables": dict(sorted(counts.items()))}


_REPLY_PREFIX_RE = re.compile(r"^re\s*:", re.IGNORECASE)


def reply_subject(prev_subject: str) -> str:
    """'Re: ' + the subject being answered, never doubled."""
    s = (prev_subject or "").strip()
    return s if _REPLY_PREFIX_RE.match(s) else f"Re: {s}"
