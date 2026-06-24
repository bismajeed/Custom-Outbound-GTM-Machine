"""Source a brief's full pool WITHOUT the technology hard-filter, then tag each
company with which target technologies it uses — so technology becomes a per-row
SIGNAL instead of a gate. No company is dropped.

Mechanism (matches the manual Apollo approach):
  1. Page the base pool: location + revenue + employees + industry keywords +
     founded + exclude — but NOT technology. This is the full pool.
  2. For each technology in the brief's ``technologies_any``, run the same query
     WITH that one technology and collect the matching company domains.
  3. For every company in the pool, the ``technologies_matched`` column lists the
     target tools it uses (empty if none). Nobody is removed.

Usage:  python scripts/tech_signal_export.py [industry]
Writes: output/<industry>/companies_with_tech_signal.csv
"""

from __future__ import annotations

import csv
import os
import sys

from dotenv import load_dotenv

load_dotenv()

from outbound.brief import load_brief                      # noqa: E402
from outbound.http import request_with_retry               # noqa: E402
from outbound.sources.apollo import (                      # noqa: E402
    _employee_ranges, _headers, _org_to_company, APOLLO_BASE,
)

PER_PAGE = 100
CAP_PAGES = 100  # safety stop (100 * 100 = 10,000 companies)


def base_payload(cf: dict) -> dict:
    """Every filter EXCEPT technology — the full pool the brief targets."""
    p: dict = {
        "organization_locations": cf.get("countries", []),
        "organization_num_employees_ranges": _employee_ranges(cf.get("employees", {})),
    }
    rev = cf.get("revenue_usd") or {}
    rr = {k: int(v) for k, v in (("min", rev.get("min")), ("max", rev.get("max")))
          if v is not None}
    if rr:
        p["revenue_range"] = rr
    ids = list(cf.get("industry_tag_ids", []))
    if ids:
        p["organization_industry_tag_ids"] = ids
    else:
        inds = list(cf.get("industries", []))
        if inds:
            p["q_organization_keyword_tags"] = inds
    fb = cf.get("founded_before")
    if fb:
        p["organization_founded_year_range"] = {"max": int(fb) - 1}
    ex = list(cf.get("exclude_keywords", []))
    if ex:
        p["q_not_organization_keyword_tags"] = ex
    return p


def paginate(cf: dict, extra: dict) -> list[dict]:
    orgs: list[dict] = []
    page = 1
    while page <= CAP_PAGES:
        payload = {"page": page, "per_page": PER_PAGE, **base_payload(cf), **extra}
        resp = request_with_retry("POST", f"{APOLLO_BASE}/mixed_companies/search",
                                  headers=_headers(), json=payload)
        data = resp.json()
        # Apollo splits results across BOTH arrays; total_entries counts both, so
        # merge them (taking one silently drops the other).
        batch = (data.get("organizations") or []) + (data.get("accounts") or [])
        if not batch:
            break
        orgs.extend(batch)
        total_pages = int(data.get("pagination", {}).get("total_pages", page) or page)
        if page >= total_pages:
            break
        page += 1
    return orgs


def main() -> None:
    industry = sys.argv[1] if len(sys.argv) > 1 else "construction"
    brief = load_brief(industry)
    cf = brief.company_filters

    # 1. Full pool (no technology filter).
    print(f"Sourcing full pool for '{industry}' (no technology filter)…")
    pool: dict[str, object] = {}
    for org in paginate(cf, {}):
        c = _org_to_company(org, brief)
        if c:
            pool[c.domain] = c
    print(f"  pool: {len(pool)} companies")

    # 2. Per-technology domain sets.
    targets = list(cf.get("technologies_any", []))
    tech_domains: dict[str, set[str]] = {t: set() for t in targets}
    for t in targets:
        for org in paginate(cf, {"currently_using_any_of_technology_uids": [t]}):
            c = _org_to_company(org, brief)
            if c:
                tech_domains[t].add(c.domain)
        print(f"  tech '{t}': {len(tech_domains[t])} companies")

    # 3. Write the CSV — every company kept; technology is a signal column.
    out_dir = os.path.join("output", industry)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "companies_with_tech_signal.csv")
    with_signal = 0
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["domain", "name", "city", "state", "founded_year",
                    "technologies_matched", "has_tech_signal"])
        for dom, c in sorted(pool.items()):
            matched = [t for t in targets if dom in tech_domains[t]]
            if matched:
                with_signal += 1
            w.writerow([dom, c.name, c.city, c.state, c.founded_year or "",
                        "; ".join(matched), "yes" if matched else "no"])

    print(f"\nWrote {path}")
    print(f"  total companies: {len(pool)}")
    print(f"  with a technology signal: {with_signal}")
    print(f"  without (kept anyway): {len(pool) - with_signal}")


if __name__ == "__main__":
    main()
