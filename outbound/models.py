"""Dataclasses passed between pipeline stages.

These are lightweight in-memory carriers. Persistence is handled by db.py;
the status strings here mirror the columns in the SQLite/Postgres schema.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


# --- Status enums (string constants, mirrored in db.py schema) ---------------

class CompanyStatus:
    SOURCED = "SOURCED"
    VALID = "VALID"
    JOB_SIGNAL = "JOB_SIGNAL"
    QUALIFIED = "QUALIFIED"
    DROPPED = "DROPPED"


class ContactStatus:
    ENRICHED = "ENRICHED"
    PERSONALIZED = "PERSONALIZED"
    QUEUED = "QUEUED"
    LOADED = "LOADED"
    SENDING = "SENDING"
    REPLIED = "REPLIED"
    BOUNCED = "BOUNCED"
    DONE = "DONE"
    SUPPRESSED = "SUPPRESSED"


@dataclass
class Company:
    domain: str
    name: str = ""
    industry: str = ""
    employees: Optional[int] = None
    revenue_usd: Optional[int] = None
    technologies: list[str] = field(default_factory=list)
    brief: str = ""
    status: str = CompanyStatus.SOURCED
    drop_reason: Optional[str] = None
    signal_summary: Optional[str] = None
    # Convenience fields used by research/validation (not all persisted).
    city: str = ""
    state: str = ""
    description: str = ""
    founded_year: Optional[int] = None


@dataclass
class Contact:
    email: str
    company_domain: str
    first_name: str = ""
    last_name: str = ""
    title: str = ""
    seniority: str = ""
    linkedin: str = ""
    brief: str = ""
    cell: Optional[str] = None          # hook id (round-robin)
    first_line: Optional[str] = None    # generated personalization
    subject: Optional[str] = None       # mode=full only
    body: Optional[str] = None          # mode=full only
    status: str = ContactStatus.ENRICHED


@dataclass
class Brief:
    """In-memory view of a validated industry brief YAML."""
    industry: str
    saved_search: str
    company_filters: dict[str, Any]
    contact_filters: dict[str, Any]
    signals: dict[str, Any]
    hooks: list[dict[str, Any]]
    personalization: dict[str, Any]
    sending: dict[str, Any]
    reservoir: dict[str, Any]
    suppression: dict[str, Any]
    messaging: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def daily_quota(self) -> int:
        return int(self.sending.get("daily_quota", 0))

    @property
    def tracking_on(self) -> bool:
        """True when the brief asks open/click tracking to stay ON.

        Note: YAML parses bare ``on``/``off`` as booleans, so handle both the
        boolean and the string forms.
        """
        val = self.sending.get("tracking", "off")
        if isinstance(val, bool):
            return val
        return str(val).strip().lower() in ("on", "true", "yes")

    @property
    def target_depth_days(self) -> int:
        return int(self.reservoir.get("target_depth_days", 7))

    @property
    def contacts_per_company(self) -> int:
        return int(self.contact_filters.get("contacts_per_company", 3))

    @property
    def personalization_mode(self) -> str:
        return str(self.personalization.get("mode", "first_line"))

    @property
    def news_lookback_days(self) -> int:
        return int(self.signals.get("news", {}).get("lookback_days", 120))


@dataclass
class RunRecord:
    run_id: str
    brief: str
    stage: str
    status: str            # started|done|failed
    counts: dict[str, Any] = field(default_factory=dict)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
