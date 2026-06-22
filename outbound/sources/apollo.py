"""Apollo source: company search + contact enrichment.

``search_companies`` honours the brief's company_filters and returns the
freshest companies first. ``enrich_contacts`` honours contact_filters and
returns only verified/likely emails (no catch-all).

Apollo REST API: https://docs.apollo.io/reference
"""

from __future__ import annotations

import os
from typing import Any, Optional

from ..http import request_with_retry
from ..models import Brief, Company, Contact, CompanyStatus

APOLLO_BASE = "https://api.apollo.io/api/v1"

# Apollo's discrete employee-count buckets. We map a brief min/max onto the
# overlapping buckets.
_EMPLOYEE_BUCKETS = [
    (1, 10), (11, 20), (21, 50), (51, 100), (101, 200),
    (201, 500), (501, 1000), (1001, 2000), (2001, 5000),
    (5001, 10000), (10001, 1_000_000),
]

# Apollo email-status values that we consider deliverable.
_GOOD_EMAIL_STATUS = {"verified", "likely", "likely_to_engage", "guessed"}


def _headers() -> dict[str, str]:
    key = os.environ.get("APOLLO_API_KEY", "")
    return {
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
        "X-Api-Key": key,
    }


def _employee_ranges(emp: dict[str, Any]) -> list[str]:
    lo = int(emp.get("min", 1))
    hi = int(emp.get("max", 1_000_000))
    ranges = []
    for blo, bhi in _EMPLOYEE_BUCKETS:
        if bhi >= lo and blo <= hi:
            ranges.append(f"{blo},{bhi}")
    return ranges


def _parse_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return None


def _org_to_company(org: dict, brief: Brief) -> Optional[Company]:
    domain = (org.get("primary_domain") or org.get("website_url") or "").strip()
    domain = domain.replace("https://", "").replace("http://", "").replace("www.", "")
    domain = domain.rstrip("/").lower()
    if not domain:
        return None

    techs = org.get("technology_names") or org.get("technologies") or []
    if isinstance(techs, list):
        tech_list = [t if isinstance(t, str) else t.get("name", "") for t in techs]
    else:
        tech_list = []

    return Company(
        domain=domain,
        name=org.get("name", ""),
        industry=org.get("industry", ""),
        employees=_parse_int(org.get("estimated_num_employees")),
        revenue_usd=_parse_int(org.get("annual_revenue")),
        technologies=[t for t in tech_list if t],
        brief=brief.industry,
        status=CompanyStatus.SOURCED,
        city=org.get("city", "") or "",
        state=org.get("state", "") or "",
        description=org.get("short_description", "") or "",
        founded_year=_parse_int(org.get("founded_year")),
    )


def search_companies(brief: Brief, limit: int) -> list[Company]:
    """Search Apollo for companies matching the brief's company_filters.

    Returns up to ``limit`` companies, freshest first. Excludes companies whose
    name/description match exclude_keywords.
    """
    if limit <= 0:
        return []

    cf = brief.company_filters
    per_page = 100
    page = 1
    out: list[Company] = []
    exclude = [k.lower() for k in cf.get("exclude_keywords", [])]

    while len(out) < limit:
        payload: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
            "organization_num_employees_ranges": _employee_ranges(
                cf.get("employees", {})
            ),
            "organization_locations": cf.get("countries", []),
        }
        # Keyword tags describe the target industry only. Technologies are a
        # FILTER on the companies, never search keywords — otherwise vendors
        # named after a tool (e.g. Bluebeam) match and pollute the results.
        #
        # Apollo's company-search technology filter is `currently_using_any_of_
        # technology_uids` and expects technology UIDs (slugs like `epic`,
        # `oracle_netsuite`), NOT display names — a display name or an unknown
        # param is silently ignored, which previously made this filter a no-op.
        # A bare brand often matches nothing (`oracle` -> 0); each product is its
        # own UID. There is no public typeahead endpoint; valid UIDs were found
        # empirically (see scripts/health_counts.py). So `technologies_any` in a
        # brief must list UIDs, not display names.
        industries = list(cf.get("industries", []))
        if industries:
            payload["q_organization_keyword_tags"] = industries
        techs = list(cf.get("technologies_any", []))
        if techs:
            payload["currently_using_any_of_technology_uids"] = techs

        resp = request_with_retry(
            "POST", f"{APOLLO_BASE}/mixed_companies/search",
            headers=_headers(), json=payload,
        )
        data = resp.json()
        orgs = data.get("organizations") or data.get("accounts") or []
        if not orgs:
            break

        for org in orgs:
            company = _org_to_company(org, brief)
            if company is None:
                continue
            blob = f"{company.name} {company.description}".lower()
            if exclude and any(kw in blob for kw in exclude):
                continue
            # founded_before filter (optional)
            fb = cf.get("founded_before")
            if fb and company.founded_year and company.founded_year >= int(fb):
                continue
            out.append(company)
            if len(out) >= limit:
                break

        pagination = data.get("pagination", {})
        total_pages = pagination.get("total_pages", page)
        if page >= total_pages:
            break
        page += 1

    return out[:limit]


# Map brief seniority labels onto Apollo's person_seniorities enum values.
_SENIORITY_MAP = {
    "c-suite": "c_suite", "c suite": "c_suite", "csuite": "c_suite",
    "c-level": "c_suite", "executive": "c_suite",
    "vp": "vp", "vice president": "vp",
    "director": "director", "head": "head", "manager": "manager",
    "owner": "owner", "founder": "founder", "partner": "partner",
    "senior": "senior",
}


def _map_seniorities(values: list[str]) -> list[str]:
    out = []
    for v in values:
        mapped = _SENIORITY_MAP.get(v.strip().lower())
        if mapped and mapped not in out:
            out.append(mapped)
    return out


def _reveal_email(person_id: str) -> tuple[str, str]:
    """People Match — reveal a verified work email for a search result.
    Returns (email, email_status); ('', '') if unavailable."""
    try:
        resp = request_with_retry(
            "POST", f"{APOLLO_BASE}/people/match",
            headers=_headers(),
            json={"id": person_id, "reveal_personal_emails": False},
        )
    except Exception:
        return "", ""
    person = resp.json().get("person") or {}
    return (person.get("email") or "").strip().lower(), \
           (person.get("email_status") or "").strip().lower()


def enrich_contacts(company: Company, brief: Brief) -> list[Contact]:
    """Find decision-maker contacts at a company and reveal their emails.

    Two-step Apollo flow: people api_search (find matching people) then people
    match (reveal verified work email). Honours contact_filters (titles,
    seniority, country) and returns up to contacts_per_company contacts with
    deliverable (verified/likely) emails only.
    """
    cf = brief.contact_filters
    want = cf.get("contacts_per_company", 3)
    allowed = {s.lower() for s in cf.get("email_status", [])}
    allowed = (allowed & _GOOD_EMAIL_STATUS) if allowed else _GOOD_EMAIL_STATUS

    payload: dict[str, Any] = {
        "page": 1,
        "per_page": max(want * 4, 10),
        "q_organization_domains_list": [company.domain],
        "person_titles": cf.get("titles_any", []),
        "person_seniorities": _map_seniorities(cf.get("seniority", [])),
        "person_locations": cf.get("countries", []),
    }

    resp = request_with_retry(
        "POST", f"{APOLLO_BASE}/mixed_people/api_search",
        headers=_headers(), json=payload,
    )
    people = resp.json().get("people") or []

    out: list[Contact] = []
    seen: set[str] = set()
    for person in people:
        if len(out) >= want:
            break
        pid = person.get("id")
        if not pid:
            continue
        email, status = _reveal_email(pid)
        if not email or "@" not in email or email in seen:
            continue
        if status and status not in allowed:
            continue  # skip catch-all / unknown
        seen.add(email)
        out.append(Contact(
            email=email,
            company_domain=company.domain,
            first_name=person.get("first_name", "") or "",
            last_name=person.get("last_name", "") or "",
            title=person.get("title", "") or "",
            seniority=person.get("seniority", "") or "",
            linkedin=person.get("linkedin_url", "") or "",
            brief=brief.industry,
        ))
    return out
