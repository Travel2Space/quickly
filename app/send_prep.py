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


def _step_is_html(campaign, position: int, is_html, body: str | None) -> bool:
    """Mirror of the sender's HTML decision, which decides whether the preheader is rendered."""
    if getattr(campaign, "send_all_as_text", False):
        return False
    if getattr(campaign, "send_first_as_text", False) and position == 0:
        return False
    html = bool(is_html) if is_html is not None else bool(re.search(r"<[a-zA-Z][^>]*>", body or ""))
    if not html and (getattr(campaign, "track_opens", False) or getattr(campaign, "track_clicks", False)):
        html = True  # tracking upgrades plain text to HTML
    return html


def _texts(campaign, position: int, subject, body, is_html, preview) -> tuple:
    """The templates the sender would render for one candidate: subject, body, preview if HTML."""
    return (subject, body, preview if _step_is_html(campaign, position, is_html, body) else None)


async def missing_vars_report(db, campaign_id: int) -> dict:
    """Before anything is sent: how many enrolled leads lack which template variables.

    Mirrors what the sender renders: standard steps contribute the default content and every
    enabled A/B variant (any of them may be drawn); a personalized step contributes, per lead,
    that lead's override, or the step's fallback when there is none.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    from app.models import Campaign, CampaignLead, CustomEmailOverride, Lead, Sequence
    from app.sender import _BUILTIN_TEMPLATE_VARS, get_lead_data

    campaign = (await db.execute(select(Campaign).where(Campaign.id == campaign_id))).scalar_one_or_none()
    own_tpl = (getattr(campaign, "list_unsubscribe_url_template", None) or "").strip()
    seqs = (await db.execute(
        select(Sequence).options(selectinload(Sequence.variants)).where(Sequence.campaign_id == campaign_id)
    )).scalars().all()
    # With an own unsubscribe URL the built-in {{unsubscribe_link}} does not exist, and the
    # URL template itself is rendered strictly for every lead.
    skip = _BUILTIN_TEMPLATE_VARS | (set() if own_tpl else {"unsubscribe_link"})

    static: set[str] = set(template_var_names(own_tpl)) if own_tpl else set()
    personalized = []
    for s in seqs:
        if getattr(s, "sequence_type", "standard") == "personalized":
            personalized.append(s)
            continue
        static.update(template_var_names(*_texts(campaign, s.position, s.subject, s.body, s.is_html, s.preview_text)))
        for v in s.variants:
            if not v.enabled:
                continue
            body = v.body if v.body else s.body
            is_html = v.is_html if v.is_html is not None else s.is_html
            preview = v.preview_text if v.preview_text is not None else s.preview_text
            subject = v.subject if v.subject is not None else s.subject
            static.update(template_var_names(*_texts(campaign, s.position, subject, body, is_html, preview)))
    static -= skip

    rows = (await db.execute(
        select(Lead, CampaignLead.id)
        .join(CampaignLead, CampaignLead.lead_id == Lead.id)
        .where(CampaignLead.campaign_id == campaign_id)
    )).all()
    overrides: dict[tuple[int, int], object] = {}
    if personalized and rows:
        ov_rows = (await db.execute(
            select(CustomEmailOverride).where(
                CustomEmailOverride.campaign_lead_id.in_([cl_id for _, cl_id in rows]),
                CustomEmailOverride.sequence_id.in_([s.id for s in personalized]),
            )
        )).scalars().all()
        overrides = {(o.campaign_lead_id, o.sequence_id): o for o in ov_rows}

    counts: dict[str, int] = {}
    leads_missing = 0
    for lead, cl_id in rows:
        needed = set(static)
        for s in personalized:
            subject, body, is_html, preview = s.subject, s.body, s.is_html, s.preview_text
            ov = overrides.get((cl_id, s.id))
            if ov is not None:
                if ov.subject is not None:
                    subject = ov.subject
                elif s.fallback_subject:
                    subject = s.fallback_subject
                if ov.body is not None:
                    body = ov.body
                elif s.fallback_body:
                    body = s.fallback_body
                if ov.is_html is not None:
                    is_html = ov.is_html
            else:
                if s.fallback_subject:
                    subject = s.fallback_subject
                if s.fallback_body:
                    body = s.fallback_body
            needed.update(template_var_names(*_texts(campaign, s.position, subject, body, is_html, preview)) - skip)
        if not needed:
            continue
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
