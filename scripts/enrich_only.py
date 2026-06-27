"""Enrich contacts on QUALIFIED companies that still need it (no sourcing/scanning).

Apollo people search + verified/likely reveal, honoring contact_filters. Spends
Apollo credits — so two guards keep spend tight:

  1. SKIP already-enriched companies (they have contacts) — never re-spend.
  2. SKIP + MARK "empty" companies: a company that yields 0 keepable contacts is
     bad data (catch-all/thin). We mark it (drop_reason="no_verified_contacts") so
     it is never re-tried in a future run. Combined with the early-exit credit-guard
     in apollo.enrich_contacts, this stops paying to re-confirm dead wells.

Usage: python scripts/enrich_only.py [industry] [limit]
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

load_dotenv()

from sqlalchemy import text                                 # noqa: E402

from outbound.brief import load_brief                       # noqa: E402
from outbound.db import Database                            # noqa: E402
from outbound.export import export_brief                    # noqa: E402
from outbound.models import CompanyStatus                   # noqa: E402
from outbound.pipeline import enrich as enrich_mod          # noqa: E402

EMPTY_MARK = "no_verified_contacts"


def main() -> None:
    industry = sys.argv[1] if len(sys.argv) > 1 else "construction"
    brief = load_brief(industry)
    db = Database(os.environ.get("DATABASE_URL", "sqlite:///outbound.db"))
    db.init()

    with db.engine.connect() as conn:
        already = {r[0] for r in conn.execute(
            text("SELECT DISTINCT company_domain FROM contacts WHERE brief=:b"),
            {"b": industry})}

    all_qual = db.companies_by_status(industry, CompanyStatus.QUALIFIED)
    # to enrich: QUALIFIED, no contacts yet, and not already marked empty
    companies = [c for c in all_qual
                 if c.domain not in already and c.drop_reason != EMPTY_MARK]
    skipped_done = sum(1 for c in all_qual if c.domain in already)
    skipped_empty = sum(1 for c in all_qual
                        if c.domain not in already and c.drop_reason == EMPTY_MARK)
    if len(sys.argv) > 2:
        companies = companies[:int(sys.argv[2])]
    n = len(companies)
    print(f"QUALIFIED: {len(all_qual)} | skip enriched: {skipped_done} | "
          f"skip known-empty: {skipped_empty} | TO ENRICH: {n}", flush=True)
    print(f"({brief.contacts_per_company} contacts each, verified/likely, early-exit on bad data)\n",
          flush=True)

    total = with_c = empty = 0
    for i, c in enumerate(companies, 1):
        try:
            contacts = enrich_mod.enrich_company(db, c, brief)
        except Exception as exc:
            print(f"[{i}/{n}] {c.name[:36]:36} ERROR {str(exc)[:50]}", flush=True)
            continue
        if contacts:
            total += len(contacts)
            with_c += 1
            seg = "signal" if (c.signal_summary or "").strip() else "free_impl"
            sample = ", ".join(f"{x.first_name} ({x.title[:16]})" for x in contacts[:2])
            print(f"[{i}/{n}] {c.name[:36]:36} +{len(contacts)}  [Σ {total} | {seg}] {sample}",
                  flush=True)
        else:
            empty += 1
            db.update_company(c.domain, drop_reason=EMPTY_MARK)  # mark so we never retry
            print(f"[{i}/{n}] {c.name[:36]:36} +0  (marked empty, won't retry)", flush=True)
        if i % 50 == 0:
            print(f"   --- {i}/{n} | {total} leads | {with_c} hit / {empty} empty ---", flush=True)

    written = export_brief(db, industry)
    print(f"\n=== ENRICHMENT COMPLETE ===")
    print(f"processed: {n}  ({with_c} yielded leads, {empty} marked empty)")
    print(f"new leads: {total}")
    print(f"CSV: {written['contacts']}")


if __name__ == "__main__":
    main()
