"""Sync inbound activity from Smartlead back into the local ledger.

Pulls replies, bounces, and unsubscribes for every campaign belonging to the
brief(s), appends them to the append-only ``events`` table, advances contact
status, and writes suppression entries so future loads never re-contact a
replied/bounced/unsubscribed person (single-touch, team-wide on shared DB).
"""

from __future__ import annotations

from rich.console import Console

from .db import Database
from .load import smartlead
from .models import ContactStatus

console = Console()


def _classify(record: dict) -> str | None:
    """Map a Smartlead lead/stat record to an event type, if actionable."""
    status = str(record.get("status") or record.get("lead_status") or "").lower()
    category = str(record.get("reply_category") or record.get("category") or "").lower()

    if record.get("is_unsubscribed") or "unsub" in status:
        return "unsubscribe"
    if record.get("is_bounced") or "bounce" in status:
        return "bounce"
    if record.get("has_replied") or "repl" in status or record.get("reply_time"):
        if any(w in category for w in ("positive", "interested", "meeting")):
            return "positive_reply"
        return "reply"
    return None


def _apply_event(db: Database, email: str, event_type: str,
                 brief_suppression: dict, payload: dict) -> None:
    db.add_event(email, event_type, payload)
    contact = db.get_contact(email)
    domain = contact.company_domain if contact else None

    if event_type == "unsubscribe":
        if brief_suppression.get("on_unsubscribe", True):
            db.add_suppression(email, "email", "unsubscribe")
        if contact:
            db.update_contact(email, status=ContactStatus.SUPPRESSED)
    elif event_type == "bounce":
        # Hard-bounce threshold: suppress after N bounce events.
        threshold = int(brief_suppression.get("hard_bounce_threshold", 2))
        bounces = _count_events(db, email, "bounce")
        if bounces >= threshold:
            db.add_suppression(email, "email", "hard_bounce")
            if contact:
                db.update_contact(email, status=ContactStatus.BOUNCED)
        elif contact:
            db.update_contact(email, status=ContactStatus.BOUNCED)
    elif event_type in ("reply", "positive_reply"):
        if contact:
            db.update_contact(email, status=ContactStatus.REPLIED)
        # A booked meeting / positive reply suppresses the whole company.
        if event_type == "positive_reply" and domain:
            db.add_suppression(domain, "domain", "meeting_booked")


def _count_events(db: Database, email: str, type_: str) -> int:
    from sqlalchemy import text
    with db.engine.connect() as conn:
        r = conn.execute(text(
            "SELECT COUNT(*) FROM events WHERE email = :e AND type = :t"
        ), {"e": email, "t": type_}).first()
    return int(r[0]) if r else 0


def sync(db: Database, briefs: list) -> dict:
    """Pull activity for all campaigns of the given briefs and update state.

    ``briefs`` is a list of Brief objects (so suppression rules apply per brief).
    Returns a counts summary.
    """
    counts = {"reply": 0, "positive_reply": 0, "bounce": 0, "unsubscribe": 0}

    for brief in briefs:
        suppression_rules = brief.suppression
        # Find this brief's campaigns by name prefix.
        try:
            campaigns = smartlead._list_campaigns()
        except Exception as exc:
            console.print(f"[yellow]Could not list campaigns:[/yellow] {exc}")
            continue

        for camp in campaigns:
            name = str(camp.get("name", ""))
            if not name.startswith(f"{brief.industry} —"):
                continue
            camp_id = str(camp.get("id"))
            for record in smartlead.fetch_campaign_replies(camp_id):
                email = (record.get("email") or
                         (record.get("lead") or {}).get("email") or "").lower()
                if not email:
                    continue
                event_type = _classify(record)
                if not event_type:
                    continue
                _apply_event(db, email, event_type, suppression_rules, record)
                counts[event_type] = counts.get(event_type, 0) + 1

    console.print(
        f"[green]Sync complete.[/green] replies={counts['reply']} "
        f"positive={counts['positive_reply']} bounces={counts['bounce']} "
        f"unsubs={counts['unsubscribe']}"
    )
    return counts
