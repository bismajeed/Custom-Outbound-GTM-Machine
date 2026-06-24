"""Enrich contacts on the already-QUALIFIED companies (no sourcing/scanning).

Runs only the enrichment stage over the brief's QUALIFIED companies — Apollo people
search + verified/likely email reveal, honoring the brief's contact_filters
(titles, seniority, email status, contacts_per_company). Spends Apollo credits.

Usage: python scripts/enrich_only.py [industry]
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

load_dotenv()

from outbound.brief import load_brief                       # noqa: E402
from outbound.db import Database                            # noqa: E402
from outbound.export import export_brief                    # noqa: E402
from outbound.models import CompanyStatus                   # noqa: E402
from outbound.pipeline import enrich as enrich_mod          # noqa: E402


def main() -> None:
    industry = sys.argv[1] if len(sys.argv) > 1 else "construction"
    brief = load_brief(industry)
    db = Database(os.environ.get("DATABASE_URL", "sqlite:///outbound.db"))
    db.init()

    companies = db.companies_by_status(industry, CompanyStatus.QUALIFIED)
    if len(sys.argv) > 2:
        companies = companies[:int(sys.argv[2])]
    n = len(companies)
    print(f"enriching {n} QUALIFIED '{industry}' companies "
          f"({brief.contacts_per_company} contacts each, verified/likely only)\n", flush=True)

    total_contacts = 0
    for i, c in enumerate(companies, 1):
        try:
            contacts = enrich_mod.enrich_company(db, c, brief)
        except Exception as exc:
            print(f"[{i}/{n}] {c.name[:34]:34} ERROR {str(exc)[:60]}", flush=True)
            continue
        total_contacts += len(contacts)
        seg = "signal" if (c.signal_summary or "").strip() else "free_impl"
        print(f"[{i}/{n}] {c.name[:34]:34} +{len(contacts)} contacts  "
              f"(total {total_contacts}, {seg})", flush=True)

    written = export_brief(db, industry)
    print(f"\n=== ENRICHMENT COMPLETE ===")
    print(f"companies processed: {n}")
    print(f"contacts found:      {total_contacts}")
    print(f"CSV: {written['contacts']}")


if __name__ == "__main__":
    main()
