"""Verify the Apollo company-match count for a brief's filters.

Reproduces what `outbound/sources/apollo.search_companies` sends to Apollo's
mixed_companies/search and prints `pagination.total_entries` — the live count of
matching companies. Read-only: per_page=1, no enrichment, no credit-consuming reveals.

Doubles as a regression check for the technology-filter fix: the tech key is
`currently_using_any_of_technology_uids` (UIDs, not display names). Run with the
key vs without to confirm the filter is actually applied.

Usage:
    python scripts/health_counts.py [industry]      # default: healthcare-admin
"""
from __future__ import annotations

import os
import sys

import requests

from outbound import brief as brief_mod
from outbound.sources.apollo import _employee_ranges

BASE = "https://api.apollo.io/api/v1"


def load_env(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _headers() -> dict:
    return {
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
        "X-Api-Key": os.environ.get("APOLLO_API_KEY", ""),
    }


def _total(payload: dict) -> int:
    payload = {"page": 1, "per_page": 1, **payload}
    r = requests.post(f"{BASE}/mixed_companies/search", headers=_headers(),
                      json=payload, timeout=40)
    r.raise_for_status()
    return r.json().get("pagination", {}).get("total_entries", 0)


def main() -> None:
    industry = sys.argv[1] if len(sys.argv) > 1 else "healthcare-admin"
    load_env()
    if not os.environ.get("APOLLO_API_KEY"):
        sys.exit("APOLLO_API_KEY not found in environment/.env")

    cf = brief_mod.load_brief(industry).company_filters
    base = {
        "organization_num_employees_ranges": _employee_ranges(cf.get("employees", {})),
        "organization_locations": cf.get("countries", []),
        "q_organization_keyword_tags": list(cf.get("industries", [])),
    }
    techs = list(cf.get("technologies_any", []))

    print(f"brief: {industry}")
    print(f"{'total':>10}   filter")
    print("-" * 60)
    print(f"{_total(base):>10,}   tags + size + location only (NO tech filter)")
    if techs:
        with_tech = {**base, "currently_using_any_of_technology_uids": techs}
        print(f"{_total(with_tech):>10,}   + tech filter ({len(techs)} UIDs) -- this is the live pool")
    else:
        print("           (brief has no technologies_any)")


if __name__ == "__main__":
    main()
