"""Contact enrichment orchestration.

Wraps ``sources.apollo.enrich_contacts`` and applies the single-touch dedup
rule: a contact whose email already exists in the DB (or is suppressed) is
skipped before any Apollo credit would be spent re-fetching it.

Contract: ``enrich_company(db, company, brief) -> list[Contact]`` — the inserted,
deduped ENRICHED contacts.
"""

from __future__ import annotations

from ..db import Database
from ..models import Brief, Company, Contact
from ..sources import apollo


def _icp_ok(title: str, keywords: list[str]) -> bool:
    """Keep a contact only if its title matches an ICP keyword (if any set).

    Pulling a wide net from Apollo then filtering here nets more true-ICP
    contacts than Apollo's narrow title filter alone.
    """
    if not keywords:
        return True
    t = (title or "").lower()
    return any(k.lower() in t for k in keywords)


def enrich_company(db: Database, company: Company, brief: Brief) -> list[Contact]:
    """Enrich one qualified company into deduped, persisted ENRICHED contacts."""
    contacts = apollo.enrich_contacts(company, brief)
    icp_keywords = brief.contact_filters.get("icp_title_keywords", [])

    inserted: list[Contact] = []
    for contact in contacts:
        # Off-ICP titles (e.g. PMs, BD) are dropped before insert.
        if not _icp_ok(contact.title, icp_keywords):
            continue
        # Single-touch guarantee: never re-load a known/suppressed contact.
        if db.contact_exists(contact.email) or db.is_suppressed(contact.email):
            continue
        if db.is_suppressed(contact.company_domain):
            continue
        if db.insert_contact(contact):
            inserted.append(contact)
    return inserted
