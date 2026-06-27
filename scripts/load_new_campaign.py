"""Load the NEW construction batch into ONE separate Smartlead campaign.

This is deliberately SEPARATE from the engine's per-segment `load` (which creates
"construction — signal" / "construction — free_implementation"). It puts ALL the
new, not-yet-loaded leads (both segments) into a single DRAFT campaign named
"Free Implementation plus Signals" so the already-running wave-1 campaigns are
never touched.

Copy: the tool-agnostic `free_implementation` messaging is used for the shared
template + follow-ups + opener A/B + UTM link — it never falsely names Procore,
so it is safe for both segments. Each lead still carries its OWN personalized
{{subject}} and {{first_line}} (generated per segment), so signal leads keep their
signal-anchored subject/opener; only the fixed offer paragraph + CTA are shared.

The campaign is created DRAFTED — nothing sends until you start it in Smartlead.
Idempotent: only QUEUED (not-yet-loaded) leads are pushed; after a successful push
they are marked LOADED, so a re-run never double-pushes.

Usage:
  python scripts/load_new_campaign.py            # dry-run: print the plan only
  python scripts/load_new_campaign.py go         # create campaign + push leads
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

from outbound.brief import load_brief                         # noqa: E402
from outbound.db import Database                              # noqa: E402
from outbound.load import smartlead                           # noqa: E402
from outbound.models import ContactStatus                     # noqa: E402

CAMPAIGN_NAME = "Free Implementation plus Signals"
COPY_SEGMENT = "free_implementation"   # tool-agnostic shared copy (safe for both)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_named_campaign(brief, name: str) -> str:
    """Create (or reuse by exact name) a DRAFT campaign, set tracking + schedule +
    the shared sequence. Mirrors smartlead.ensure_campaign but with a custom name
    and a fixed copy segment, so it never collides with the engine's campaigns."""
    existing_id = None
    for camp in smartlead._list_campaigns():
        if str(camp.get("name", "")).strip() == name:
            existing_id = str(camp.get("id"))
            break

    if existing_id is None:
        resp = smartlead.request_with_retry(
            "POST", smartlead._url("/campaigns/create"), json={"name": name})
        body = resp.json()
        existing_id = str(body.get("id") or body.get("campaign_id") or
                          (body.get("data") or {}).get("id"))
        print(f"  created DRAFT campaign '{name}' -> {existing_id}")
    else:
        print(f"  reusing existing campaign '{name}' -> {existing_id}")

    smartlead._set_tracking(brief, existing_id)
    try:
        smartlead._set_schedule(brief, existing_id)
    except Exception as exc:
        print(f"  schedule warning: {exc}")
    # Shared sequence: tool-agnostic opener A/B (no-link A / UTM-link B) + follow-ups.
    smartlead.ensure_sequence(brief, existing_id, COPY_SEGMENT)
    return existing_id


def main() -> None:
    go = len(sys.argv) > 1 and sys.argv[1].lower() == "go"
    brief = load_brief("construction")
    db = Database(os.environ.get("DATABASE_URL", "sqlite:///outbound.db"))
    db.init()

    contacts = db.contacts_by_status(brief.industry, ContactStatus.QUEUED)
    sig = sum(1 for c in contacts if c.cell == "signal")
    free = len(contacts) - sig
    track = "ON" if brief.tracking_on else "OFF"

    print(f"=== NEW CAMPAIGN LOAD: '{CAMPAIGN_NAME}' ===")
    print(f"new (QUEUED, not-yet-loaded) leads: {len(contacts)}  "
          f"(signal {sig} / free {free})")
    print(f"shared copy segment: {COPY_SEGMENT} (tool-agnostic, UTM + opener A/B)")
    print(f"tracking: {track} | schedule: {brief.sending.get('days')} "
          f"{brief.sending.get('window_local')} | cap/day: {brief.daily_quota}")
    print(f"campaign will be created DRAFTED — nothing sends until you start it.\n")

    if not contacts:
        print("Nothing to load (reservoir empty).")
        return
    if not go:
        print("DRY RUN — re-run with `go` to create the campaign and push leads:")
        print("  python scripts/load_new_campaign.py go")
        return

    # Suppression to the workspace before any push.
    supp = db.all_suppression_keys()
    if supp:
        try:
            smartlead.apply_suppression(supp)
            print(f"  applied {len(supp)} suppression keys to workspace")
        except Exception as exc:
            print(f"  suppression warning: {exc}")

    campaign_id = _ensure_named_campaign(brief, CAMPAIGN_NAME)

    # Final per-contact suppression guard (single-touch).
    sendable = [c for c in contacts
                if not db.is_suppressed(c.email)
                and not db.is_suppressed(c.company_domain)]
    skipped = len(contacts) - len(sendable)
    if skipped:
        print(f"  skipped {skipped} suppressed leads")

    pushed = smartlead.push_leads(campaign_id, sendable)
    for c in sendable:
        db.update_contact(c.email, status=ContactStatus.LOADED, loaded_at=_now_iso())
        db.add_event(c.email, "sent", {"campaign_id": campaign_id,
                                       "campaign_name": CAMPAIGN_NAME, "cell": c.cell})

    print(f"\n=== DONE ===")
    print(f"pushed {pushed} leads into DRAFT campaign {campaign_id} ('{CAMPAIGN_NAME}')")
    print(f"review + start it manually in Smartlead. The live wave-1 campaigns were not touched.")


if __name__ == "__main__":
    main()
