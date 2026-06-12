"""Ingest pre-validated companies from a CSV into the pipeline.

For lists that have already been sourced + domain-validated elsewhere (e.g. the
Apollo domain-validator output), this skips the source/validate stages and drops
the companies straight in as QUALIFIED so the runner can go enrich → personalize.

Column mapping is flexible; it expects at least a company name and a domain.
"""

from __future__ import annotations

import csv
from typing import Optional

from .db import Database
from .models import Brief, Company, CompanyStatus

# Accepted header aliases (lowercased).
_NAME_COLS = ["company_name", "name", "company", "organization"]
_DOMAIN_COLS = ["corrected_domain", "company_domain", "domain", "website"]
_CITY_COLS = ["company_city", "city"]
_STATE_COLS = ["company_state", "state", "state/province"]
_STATUS_COLS = ["domain_status", "status"]


def _normalize_domain(raw: str) -> str:
    d = (raw or "").strip().lower()
    for prefix in ("https://", "http://", "www."):
        if d.startswith(prefix):
            d = d[len(prefix):]
    return d.rstrip("/")


def _pick(row: dict, cols: list[str]) -> str:
    for c in cols:
        if c in row and row[c]:
            return str(row[c]).strip()
    return ""


def load_companies_from_csv(path: str, brief: Brief,
                            offset: int = 0, limit: Optional[int] = None) -> list[Company]:
    """Read a validated CSV into Company objects (status QUALIFIED).

    Prefers a corrected_domain over the raw domain. Skips rows whose
    domain_status marks them unverifiable/missing, and rows with no domain.
    ``offset``/``limit`` slice the list (used to split a file across campaigns).
    """
    companies: list[Company] = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        norm_rows = [{(k or "").strip().lower(): v for k, v in r.items()} for r in reader]

    for row in norm_rows:
        status = _pick(row, _STATUS_COLS).upper()
        if status in {"UNVERIFIABLE", "MISSING", "INVALID"}:
            continue
        domain = _normalize_domain(_pick(row, _DOMAIN_COLS))
        name = _pick(row, _NAME_COLS)
        if not domain or not name:
            continue
        companies.append(Company(
            domain=domain,
            name=name,
            brief=brief.industry,
            status=CompanyStatus.QUALIFIED,   # pre-validated → skip source/validate
            city=_pick(row, _CITY_COLS),
            state=_pick(row, _STATE_COLS),
        ))

    end = (offset + limit) if limit is not None else None
    return companies[offset:end]


def ingest(db: Database, brief: Brief, path: str,
           offset: int = 0, limit: Optional[int] = None) -> dict:
    """Load + dedup-insert validated companies as QUALIFIED. Returns counts."""
    companies = load_companies_from_csv(path, brief, offset=offset, limit=limit)
    inserted = skipped = 0
    for c in companies:
        if db.insert_company(c):
            inserted += 1
        else:
            skipped += 1  # already in DB or suppressed (single-touch)
    return {"considered": len(companies), "inserted": inserted, "skipped": skipped}
