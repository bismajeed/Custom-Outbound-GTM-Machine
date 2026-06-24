"""Personalize the ENRICHED contacts (generate subject + first line, assign segment).

Anthropic (Haiku) only — NO Apollo credits. Parallelized for speed; DB writes are
done single-threaded after generation. Writes the final contacts CSV.

Usage: python scripts/personalize_only.py [industry] [workers]
"""

from __future__ import annotations

import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv

load_dotenv()

from outbound.brief import load_brief                       # noqa: E402
from outbound.db import Database                            # noqa: E402
from outbound.export import export_brief                    # noqa: E402
from outbound.models import ContactStatus                   # noqa: E402
from outbound.pipeline import personalize as personalize_mod  # noqa: E402

_lock = threading.Lock()
_done = 0


def main() -> None:
    industry = sys.argv[1] if len(sys.argv) > 1 else "construction"
    workers = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    brief = load_brief(industry)
    db = Database(os.environ.get("DATABASE_URL", "sqlite:///outbound.db"))
    db.init()

    contacts = db.contacts_by_status(industry, ContactStatus.ENRICHED)
    total = len(contacts)
    print(f"personalizing {total} contacts ({workers} workers, Anthropic only)\n", flush=True)

    # Cache companies (read-only) so threads don't re-fetch.
    comp_cache = {}
    for c in contacts:
        if c.company_domain not in comp_cache:
            comp_cache[c.company_domain] = db.get_company(c.company_domain)

    def work(contact):
        global _done
        company = comp_cache.get(contact.company_domain)
        if company is None:
            return None
        try:
            updated, cost = personalize_mod.personalize_detailed(contact, company, brief)
        except Exception as exc:
            with _lock:
                _done += 1
            return ("err", contact.email, str(exc)[:60])
        with _lock:
            _done += 1
            d = _done
        if d % 100 == 0:
            print(f"  {d}/{total} personalized", flush=True)
        return ("ok", updated, cost)

    results = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(work, c) for c in contacts]
        for f in as_completed(futs):
            r = f.result()
            if r:
                results.append(r)

    # Single-threaded DB writes.
    cost = 0.0
    seg = {"signal": 0, "free_implementation": 0}
    for r in results:
        if r[0] != "ok":
            continue
        updated, c = r[1], r[2]
        cost += c
        seg[updated.cell] = seg.get(updated.cell, 0) + 1
        db.update_contact(updated.email, cell=updated.cell, first_line=updated.first_line,
                          subject=updated.subject, body=updated.body,
                          status=ContactStatus.QUEUED)

    written = export_brief(db, industry)
    print(f"\n=== PERSONALIZATION COMPLETE ===")
    print(f"personalized: {sum(1 for r in results if r[0] == 'ok')} / {total}")
    print(f"  signal segment:              {seg.get('signal', 0)}")
    print(f"  free_implementation segment: {seg.get('free_implementation', 0)}")
    print(f"cost: ${cost:.2f} (Anthropic; $0 Apollo)")
    print(f"CSV: {written['contacts']}")


if __name__ == "__main__":
    main()
