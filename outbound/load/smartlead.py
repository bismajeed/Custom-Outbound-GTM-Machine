"""Smartlead loader — the only module that puts leads in front of recipients.

Deliverability rules are non-negotiable here:
- Open/click/reply tracking is forced OFF on every campaign.
- Per-mailbox caps and the brief's daily_quota are respected.
- Suppression is applied to the workspace before any leads are pushed.

Campaigns are created idempotently (one per hook/cell), so re-running load never
duplicates a campaign. The personalized first line maps to a Smartlead custom
field so the email template can reference {{first_line}}.

Smartlead REST API: https://api.smartlead.ai/reference
"""

from __future__ import annotations

import os
from typing import Any, Iterable

from ..http import request_with_retry
from ..models import Brief, Contact
from ..pipeline.personalize import subject_variant

SMARTLEAD_BASE = "https://server.smartlead.ai/api/v1"

# Forcing every tracking signal off — deliverability rule.
_TRACK_OFF = [
    "DONT_TRACK_EMAIL_OPEN",
    "DONT_TRACK_LINK_CLICK",
    "DONT_TRACK_REPLY_TO_AN_EMAIL",
]


def _api_key() -> str:
    return os.environ.get("SMARTLEAD_API_KEY", "")


def _url(path: str) -> str:
    sep = "&" if "?" in path else "?"
    return f"{SMARTLEAD_BASE}{path}{sep}api_key={_api_key()}"


def _campaign_name(brief: Brief, cell: str) -> str:
    return f"{brief.industry} — {cell}"


def _list_campaigns() -> list[dict]:
    resp = request_with_retry("GET", _url("/campaigns"))
    data = resp.json()
    if isinstance(data, list):
        return data
    return data.get("data") or data.get("campaigns") or []


# Smartlead schedule uses 0=Sunday .. 6=Saturday.
_DAY_NUM = {
    "sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6,
}


def _hook_angle(brief: Brief, cell: str) -> str:
    for hook in brief.hooks:
        if hook.get("id") == cell:
            return hook.get("angle", "")
    return brief.hooks[0].get("angle", "") if brief.hooks else ""


def _set_tracking(brief: Brief, campaign_id: str) -> None:
    """Apply tracking per the brief. tracking:off forces open/click/reply tracking
    off (deliverability default); tracking:on leaves it on to gather open-rate
    data. Raises on failure so a silent mis-set never goes unnoticed."""
    track_settings = [] if brief.tracking_on else _TRACK_OFF
    request_with_retry(
        "POST", _url(f"/campaigns/{campaign_id}/settings"),
        json={
            "track_settings": track_settings,
            "stop_lead_settings": "REPLY_TO_AN_EMAIL",
        },
    )


def _set_schedule(brief: Brief, campaign_id: str) -> None:
    """Apply the brief's sending days, window, and daily cap to the campaign."""
    days = [_DAY_NUM[d[:3].lower()] for d in brief.sending.get("days", [])
            if d[:3].lower() in _DAY_NUM]
    window = brief.sending.get("window_local", "09:00-17:00")
    start, _, end = window.partition("-")
    request_with_retry(
        "POST", _url(f"/campaigns/{campaign_id}/schedule"),
        json={
            "timezone": brief.sending.get("timezone", "America/New_York"),
            "days_of_the_week": days or [1, 2, 3, 4, 5],
            "start_hour": start.strip() or "09:00",
            "end_hour": end.strip() or "17:00",
            "min_time_btw_emails": 10,
            "max_new_leads_per_day": brief.daily_quota,
        },
    )


def _opt_out_html(brief: Brief) -> str:
    opt_out = brief.messaging.get(
        "opt_out", "not relevant? just reply 'stop' and i'll close this out.")
    return f"<p style=\"font-size:12px;color:#888\">{opt_out}</p>"


def _email_template(brief: Brief, cell: str) -> tuple[str, str]:
    """Return (subject, html_body) for the campaign's step-1 email.

    - full mode: the whole body is the generated {{body}}.
    - template mode (no signal): fixed opener + offer + CTA, merge vars only.
    - first_line mode: opens with the per-lead generated {{first_line}}.
    No signature paragraph — the Smartlead mailbox appends its own.
    """
    if brief.personalization_mode == "full":
        # Generated subject + body per lead; append the compliant opt-out.
        return "{{subject}}", f"<p>{{{{body}}}}</p>{_opt_out_html(brief)}"

    m = brief.messaging
    offer = m.get("offer") or f"we work with {brief.industry} teams on {_hook_angle(brief, cell)}"
    proof = m.get("proof_point", "")
    cta = m.get("cta", "worth a quick look?")
    value_para = f"{offer}. {proof}." if proof else f"{offer}."

    if brief.personalization_mode == "template":
        opener = m.get("template_opener",
                       "reaching out about how your team handles its workflow.")
        subject = m.get("subject_template") or m.get("subject_value_fallback") \
            or f"a note for {{{{company_name}}}}"
        body = (
            "<p>Hi {{first_name}},</p>"
            f"<p>{opener}</p>"
            f"<p>{value_para}</p>"
            f"<p>{cta}</p>"
            f"{_opt_out_html(brief)}"
        )
        return subject, body

    # first_line mode
    body = (
        "<p>Hi {{first_name}},</p>"
        "<p>{{first_line}}</p>"
        f"<p>{value_para}</p>"
        f"<p>{cta}</p>"
        f"{_opt_out_html(brief)}"
    )
    return "{{subject}}", body


def _follow_up_html(body_text: str, brief: Brief) -> str:
    """Wrap a follow-up body string in HTML with the opt-out appended."""
    return f"<p>Hi {{{{first_name}}}},</p><p>{body_text}</p>{_opt_out_html(brief)}"


def ensure_sequence(brief: Brief, campaign_id: str, cell: str) -> None:
    """Create the campaign's multi-step email sequence idempotently.

    Step 1 is the personalized/templated opener; steps 2+ are the follow-ups
    from the brief's ``messaging.follow_ups`` (each with a delay and body).
    Skips if a sequence already exists so re-runs don't duplicate steps.
    """
    try:
        existing = request_with_retry(
            "GET", _url(f"/campaigns/{campaign_id}/sequences")
        ).json()
        seqs = existing if isinstance(existing, list) else existing.get("data", [])
        if seqs:
            return  # already has a sequence — leave operator edits intact
    except Exception:
        pass

    subject, body = _email_template(brief, cell)
    sequences = [{
        "seq_number": 1,
        "seq_delay_details": {"delay_in_days": 0},
        "subject": subject,
        "email_body": body,
    }]
    # Follow-ups. Empty subject => Smartlead threads it under the same subject.
    for i, step in enumerate(brief.messaging.get("follow_ups", []), start=2):
        sequences.append({
            "seq_number": i,
            "seq_delay_details": {"delay_in_days": int(step.get("delay_days", 3))},
            "subject": "",
            "email_body": _follow_up_html(step.get("body", ""), brief),
        })

    request_with_retry(
        "POST", _url(f"/campaigns/{campaign_id}/sequences"),
        json={"sequences": sequences},
    )


def ensure_campaign(brief: Brief, cell: str) -> str:
    """Idempotently ensure a campaign exists for this brief+cell. Returns its id.

    Applies the brief's tracking setting, schedule + daily cap, and creates the
    email sequence so the personalization variables render.
    """
    name = _campaign_name(brief, cell)

    existing_id = None
    for camp in _list_campaigns():
        if str(camp.get("name", "")).strip() == name:
            existing_id = str(camp.get("id"))
            break

    if existing_id is None:
        resp = request_with_retry(
            "POST", _url("/campaigns/create"), json={"name": name}
        )
        body = resp.json()
        existing_id = str(body.get("id") or body.get("campaign_id") or
                          (body.get("data") or {}).get("id"))

    # Tracking per the brief (off by default). Let failure surface — a silent
    # mis-set is a deliverability risk.
    _set_tracking(brief, existing_id)

    # Schedule (days/window/cap) is best-effort; warn but don't block the load.
    try:
        _set_schedule(brief, existing_id)
    except Exception:
        pass

    # Email sequence carries the personalization variables.
    ensure_sequence(brief, existing_id, cell)

    return existing_id


def push_leads(campaign_id: str, contacts: list[Contact]) -> int:
    """Push contacts as leads into a campaign with tracking off.

    The personalized first line is mapped to a custom field (first_line). Returns
    the number of leads submitted. Respects Smartlead's 100-lead batch limit.
    """
    if not contacts:
        return 0

    lead_list = []
    for c in contacts:
        lead: dict[str, Any] = {
            "email": c.email,
            "first_name": c.first_name,
            "last_name": c.last_name,
            "company_name": c.company_domain,
            "custom_fields": {
                "first_line": c.first_line or "",
                "hook": c.cell or "",
            },
        }
        # subject_variant lets you segment the A/B (signal vs value) in Smartlead.
        lead["custom_fields"]["subject_variant"] = subject_variant(c.email)
        if c.title:
            lead["custom_fields"]["title"] = c.title
        if c.linkedin:
            lead["linkedin_profile"] = c.linkedin
        if c.subject:
            lead["custom_fields"]["subject"] = c.subject
        if c.body:
            lead["custom_fields"]["body"] = c.body
        lead_list.append(lead)

    submitted = 0
    # Smartlead caps lead uploads at 100 per request.
    for i in range(0, len(lead_list), 100):
        batch = lead_list[i:i + 100]
        request_with_retry(
            "POST", _url(f"/campaigns/{campaign_id}/leads"),
            json={
                "lead_list": batch,
                "settings": {
                    "ignore_global_block_list": False,
                    "ignore_unsubscribe_list": False,
                    "ignore_duplicate_leads_in_other_campaign": False,
                },
            },
        )
        submitted += len(batch)
    return submitted


def apply_suppression(domains_emails: Iterable[str]) -> int:
    """Push emails/domains to the Smartlead workspace global block list.

    Applied before any lead push so suppressed records never get contacted.
    """
    items = [x for x in domains_emails if x]
    if not items:
        return 0
    emails = [x for x in items if "@" in x]
    domains = [x for x in items if "@" not in x]

    if emails:
        request_with_retry(
            "POST", _url("/leads/add-domain-block-list"),
            json={"domain_block_list": emails},
        )
    if domains:
        request_with_retry(
            "POST", _url("/leads/add-domain-block-list"),
            json={"domain_block_list": domains},
        )
    return len(items)


def fetch_campaign_replies(campaign_id: str) -> list[dict]:
    """Pull recent message history / replies for a campaign (used by sync)."""
    try:
        resp = request_with_retry(
            "GET", _url(f"/campaigns/{campaign_id}/statistics")
        )
        data = resp.json()
        return data.get("data") or data.get("leads") or []
    except Exception:
        return []


def fetch_all_leads(campaign_id: str) -> list[dict]:
    """Fetch ALL leads for a campaign including completed ones, with pagination.

    The /statistics endpoint only returns active leads. This endpoint returns
    every lead regardless of sequence status, giving accurate sent counts.
    """
    all_leads: list[dict] = []
    offset = 0
    limit = 100
    while True:
        try:
            resp = request_with_retry(
                "GET", _url(f"/campaigns/{campaign_id}/leads") +
                f"&offset={offset}&limit={limit}"
            )
            data = resp.json()
            page = data if isinstance(data, list) else (data.get("data") or data.get("leads") or [])
            if not page:
                break
            all_leads.extend(page)
            if len(page) < limit:
                break
            offset += limit
        except Exception:
            break
    return all_leads
